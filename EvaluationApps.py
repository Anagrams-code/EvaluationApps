# FILE: app.py
"""
All-in-one Streamlit App: Goal -> Approval -> Evaluation -> 1on1 -> CSV
Streamlit Community Cloud + PostgreSQL

Integrated fixes:
- 年度をログイン時に入力（4桁の数値）→ セッション固定（全ページ共通）
- DBは既存のyear列を使用（年度運用）
- year未定義エラー修正（eval_input_self等）
- SQLite既存DBのスキーマ差分を軽量マイグレーションで吸収
  - employees.email が無いDBでも落ちない
- 初期管理者（000001, 425025）の初期PWは ChangeMe_1234（Secrets未設定でも）
- CSVアップロードの文字コード問題（UTF-8/CP932）を吸収
- 従業員マスタに email 列を追加（CSV/手動）
- 目標入力UI刷新（business最大5 / development最大2）
  - business: 対象業務 / 達成したい結果 / 期限(日付) / 部署ゴールとの関連性 / 実行計画(任意)
  - development: なりたい人物像/身につけたいスキル / 現在の自分とのギャップ / 行動 / 実行計画(任意) / 完了時期(任意: 日付)
- SMART理論の説明をサイドバーに表示
- Streamlit rerun による SQLAlchemy セッション不整合対策（merge）
"""

from __future__ import annotations

import csv
import os
import secrets
import smtplib
from dataclasses import dataclass, asdict
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import StringIO
from typing import Optional, Literal, Callable, Dict, List, Any, Tuple

import matplotlib.pyplot as plt
import streamlit as st
from passlib.context import CryptContext
from sqlalchemy import (
    create_engine,
    String,
    Boolean,
    DateTime,
    Integer,
    Text,
    select,
    UniqueConstraint,
    ForeignKey,
    event,
    inspect,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    sessionmaker,
    relationship,
)

# ----------------------------
# Constants & Branding
# ----------------------------
APP_TITLE = "Goal Management System - Stanley Black & Decker"

BRAND_COLORS = {
    "primary": "#FFCC00",
    "secondary": "#000000",
    "dark_gray": "#333333",
    "light_gray": "#F5F5F5",
    "border_gray": "#E8E8E8",
    "success": "#28a745",
    "warning": "#ffc107",
    "error": "#dc3545",
    "info": "#17a2b8",
}

PWD_CONTEXT = CryptContext(
    schemes=["pbkdf2_sha256", "bcrypt"],
    default="pbkdf2_sha256",
    deprecated="auto",
)

Role = Literal["HR管理者", "評価者", "入力者"]
ROLE_TO_FLAG = {"HR管理者": "role_admin", "評価者": "role_manager", "入力者": "role_employee"}
ROLE_TO_KEY = {"HR管理者": "admin", "評価者": "manager", "入力者": "employee"}

GOAL_STATUSES = {
    "draft": "下書き",
    "submitted": "上長承認待ち",
    "manager_returned": "上長差し戻し",
    "manager_approved": "HR確認待ち",
    "hr_returned": "HR差し戻し",
    "hr_approved": "公開（確定）",
}

EVAL_STATUSES = {
    "draft": "下書き",
    "submitted_self": "上長評価待ち",
    "manager_returned": "上長差し戻し",
    "manager_submitted": "HR確認待ち",
    "hr_returned": "HR差し戻し",
    "hr_approved": "公開（確定）",
}

WHAT_THRESHOLDS = {"exceeds": 130.0, "meets": 95.0}

HOW_TOTAL_MAX = 160
HOW_EXCEEDS_RATIO = 0.9
HOW_MEETS_RATIO = 0.6

HOW_CATEGORIES: List[Tuple[str, str]] = [
    ("innovation_courage", "革新さと勇気"),
    ("agility_performance", "機敏さとパフォーマンス"),
    ("inclusion_collaboration", "包括性とコラボレーション"),
    ("integrity", "誠実さ"),
    ("customer_focus", "顧客志向"),
    ("impact_on_others", "他者への影響"),
    ("change_leadership", "チェンジリーダーシップ"),
    ("efficiency", "効率化"),
]

BUSINESS_GOAL_MAX = 5
DEVELOPMENT_GOAL_MAX = 2


# ----------------------------
# Secrets / Config
# ----------------------------
def get_secret(key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.getenv(key, default)


def database_url() -> str:
    url = get_secret("DATABASE_URL")
    if not url:
        return "sqlite:///./data/app.db"
    return url


def admin_seed_password() -> str:
    return get_secret("ADMIN_SEED_PASSWORD", "ChangeMe_1234") or "ChangeMe_1234"


DEFAULT_PASSWORD_IF_BLANK = "ChangeMe_1234"


# ----------------------------
# Year handling (Login -> Session fixed)
# ----------------------------
def set_selected_year(year: int, *, force: bool = False) -> None:
    if not force and st.session_state.get("auth_user") and st.session_state.get("selected_year") is not None:
        return
    st.session_state["selected_year"] = int(year)


def get_selected_year() -> int:
    y = st.session_state.get("selected_year")
    if y is None:
        y = datetime.utcnow().year
        st.session_state["selected_year"] = int(y)
    return int(y)


def clear_selected_year() -> None:
    st.session_state.pop("selected_year", None)


def year_input_login(default_year: int) -> int:
    return int(
        st.number_input(
            "年度（4桁）",
            min_value=2000,
            max_value=2100,
            value=int(default_year),
            step=1,
            format="%d",
        )
    )


# ----------------------------
# Email Support
# ----------------------------
def send_email(to_email: Optional[str], subject: str, body: str) -> bool:
    if not to_email or not to_email.strip():
        return False

    try:
        smtp_server = get_secret("SMTP_SERVER", "smtp.gmail.com")
        smtp_port = int(get_secret("SMTP_PORT", "587") or "587")
        smtp_user = get_secret("SMTP_USERNAME")
        smtp_pass = get_secret("SMTP_PASSWORD")
        from_email = get_secret("SMTP_FROM_EMAIL", smtp_user)

        if not smtp_user or not smtp_pass:
            st.session_state["email_debug"] = f"[Email Skipped] To: {to_email}\nSubject: {subject}\n{body}"
            return False

        msg = MIMEMultipart()
        msg["From"] = from_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        import ssl

        tls_context = ssl.create_default_context()
        allow_unverified = (get_secret("SMTP_ALLOW_UNVERIFIED", "") or "").lower() in ("1", "true", "yes")
        if allow_unverified:
            tls_context.check_hostname = False
            tls_context.verify_mode = ssl.CERT_NONE

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls(context=tls_context)
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        return True
    except Exception as e:
        st.session_state["email_error"] = str(e)
        return False


# ----------------------------
# DB Models
# ----------------------------
class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = (UniqueConstraint("emp_no", name="uq_employees_emp_no"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    emp_no: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    department: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    role_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    role_manager: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    role_employee: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    manager_emp_no: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    manager_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    password_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Goal(Base):
    __tablename__ = "goals"
    __table_args__ = (UniqueConstraint("employee_emp_no", "year", name="uq_goals_employee_year"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    employee_emp_no: Mapped[str] = mapped_column(String(32), ForeignKey("employees.emp_no"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    items: Mapped[List["GoalItem"]] = relationship(back_populates="goal", cascade="all, delete-orphan")
    approvals: Mapped[List["GoalApproval"]] = relationship(back_populates="goal", cascade="all, delete-orphan")


class GoalItem(Base):
    __tablename__ = "goal_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    goal_id: Mapped[int] = mapped_column(Integer, ForeignKey("goals.id"), nullable=False)

    type: Mapped[str] = mapped_column(String(16), nullable=False)  # business/development

    # 共通（画面ではラベルを差し替える）
    specific: Mapped[str] = mapped_column(Text, nullable=False)      # 対象業務 / なりたい人物像
    measurable: Mapped[str] = mapped_column(Text, nullable=False)    # 達成したい結果 / ギャップ
    relevant: Mapped[str] = mapped_column(Text, nullable=False)      # 部署ゴールとの関連性 / 行動
    achievable: Mapped[str] = mapped_column(Text, nullable=False)    # 実行計画(任意)
    time_bound: Mapped[str] = mapped_column(Text, nullable=False)    # 期限(日付ISO) / 完了時期(日付ISO, 任意)

    # developmentだけ追加情報が必要なら使用（今回は未使用のまま保持）
    career_vision: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # businessのみ
    weight: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)           # 0..100
    achieved_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True) # 0..200

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    goal: Mapped[Goal] = relationship(back_populates="items")


class GoalApproval(Base):
    __tablename__ = "goal_approvals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    goal_id: Mapped[int] = mapped_column(Integer, ForeignKey("goals.id"), nullable=False)

    stage: Mapped[str] = mapped_column(String(16), nullable=False)  # employee/manager/hr
    action: Mapped[str] = mapped_column(String(16), nullable=False) # save/submit/approve/return
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)

    actor_emp_no: Mapped[str] = mapped_column(String(32), nullable=False)
    acted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    goal: Mapped[Goal] = relationship(back_populates="approvals")


class Evaluation(Base):
    __tablename__ = "evaluations"
    __table_args__ = (UniqueConstraint("employee_emp_no", "year", name="uq_evals_employee_year"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    employee_emp_no: Mapped[str] = mapped_column(String(32), ForeignKey("employees.emp_no"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    goal_id: Mapped[int] = mapped_column(Integer, ForeignKey("goals.id"), nullable=False)

    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)

    what_self: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    what_manager: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    what_final: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    how_self: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    how_manager: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    how_final: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    self_comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    manager_comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    hr_comment: Mapped[str] = mapped_column(Text, default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    answers: Mapped[List["HowAnswer"]] = relationship(back_populates="evaluation", cascade="all, delete-orphan")
    approvals: Mapped[List["EvaluationApproval"]] = relationship(back_populates="evaluation", cascade="all, delete-orphan")
    oneonone: Mapped[Optional["OneOnOne"]] = relationship(back_populates="evaluation", cascade="all, delete-orphan")


class HowQuestion(Base):
    __tablename__ = "how_questions"
    __table_args__ = (UniqueConstraint("category_key", "question_no", name="uq_howq_cat_no"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    category_key: Mapped[str] = mapped_column(String(64), nullable=False)
    category_label: Mapped[str] = mapped_column(String(128), nullable=False)
    question_no: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..5
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class HowAnswer(Base):
    __tablename__ = "how_answers"
    __table_args__ = (UniqueConstraint("evaluation_id", "rater", "category_key", "question_no", name="uq_howa"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    evaluation_id: Mapped[int] = mapped_column(Integer, ForeignKey("evaluations.id"), nullable=False)
    rater: Mapped[str] = mapped_column(String(16), nullable=False)  # self/manager
    category_key: Mapped[str] = mapped_column(String(64), nullable=False)
    question_no: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..4

    evaluation: Mapped[Evaluation] = relationship(back_populates="answers")


class EvaluationApproval(Base):
    __tablename__ = "evaluation_approvals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    evaluation_id: Mapped[int] = mapped_column(Integer, ForeignKey("evaluations.id"), nullable=False)

    stage: Mapped[str] = mapped_column(String(16), nullable=False)  # self/manager/hr
    action: Mapped[str] = mapped_column(String(16), nullable=False) # save/submit/approve/return
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)

    actor_emp_no: Mapped[str] = mapped_column(String(32), nullable=False)
    acted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    evaluation: Mapped[Evaluation] = relationship(back_populates="approvals")


class OneOnOne(Base):
    __tablename__ = "one_on_ones"
    __table_args__ = (UniqueConstraint("evaluation_id", name="uq_oneonone_eval"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    evaluation_id: Mapped[int] = mapped_column(Integer, ForeignKey("evaluations.id"), nullable=False)

    manager_emp_no: Mapped[str] = mapped_column(String(32), nullable=False)
    employee_emp_no: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)  # draft/proposed/confirmed

    slot1: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    slot2: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    slot3: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    selected_slot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 1..3
    location: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    evaluation: Mapped[Evaluation] = relationship(back_populates="oneonone")


# ----------------------------
# DB init
# ----------------------------
_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(_data_dir, exist_ok=True)


def _get_engine():
    db_url = database_url()
    if db_url.startswith("sqlite://"):
        engine = create_engine(
            db_url,
            echo=False,
            future=True,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    return create_engine(db_url, echo=False, future=True, pool_pre_ping=True)


ENGINE = _get_engine()
SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, autocommit=False, future=True)


def _sqlite_table_exists(conn, table: str) -> bool:
    row = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def migrate_sqlite_schema_if_needed() -> None:
    if not database_url().startswith("sqlite://"):
        return

    with ENGINE.begin() as conn:
        if not _sqlite_table_exists(conn, "employees"):
            return

        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(employees)").fetchall()]

        if "email" not in cols:
            conn.exec_driver_sql("ALTER TABLE employees ADD COLUMN email VARCHAR(255)")
        if "manager_email" not in cols:
            conn.exec_driver_sql("ALTER TABLE employees ADD COLUMN manager_email VARCHAR(255)")


def init_db() -> None:
    Base.metadata.create_all(ENGINE)
    migrate_sqlite_schema_if_needed()
    seed_admin_if_needed()
    seed_how_questions_if_needed()


def seed_admin_if_needed() -> None:
    with SessionLocal() as db:
        existing = db.execute(select(Employee).where(Employee.emp_no == "000001")).scalar_one_or_none()
        if existing:
            hr_admin_425025 = db.execute(select(Employee).where(Employee.emp_no == "425025")).scalar_one_or_none()
            if not hr_admin_425025:
                db.add(
                    Employee(
                        emp_no="425025",
                        name="土田光範",
                        department="HR",
                        email="tsuchida@company.example.com",
                        password_hash=PWD_CONTEXT.hash(admin_seed_password()),
                        active=True,
                        role_admin=True,
                        role_manager=False,
                        role_employee=False,
                        manager_emp_no=None,
                        manager_email=None,
                        must_change_password=True,
                        password_updated_at=None,
                        last_login_at=None,
                    )
                )
                db.commit()
            return

        db.add(
            Employee(
                emp_no="000001",
                name="HR 管理者",
                department="HR",
                email="admin@company.example.com",
                password_hash=PWD_CONTEXT.hash(admin_seed_password()),
                active=True,
                role_admin=True,
                role_manager=False,
                role_employee=False,
                manager_emp_no=None,
                manager_email=None,
                must_change_password=True,
                password_updated_at=None,
                last_login_at=None,
            )
        )

        hr_admin_425025 = db.execute(select(Employee).where(Employee.emp_no == "425025")).scalar_one_or_none()
        if not hr_admin_425025:
            db.add(
                Employee(
                    emp_no="425025",
                    name="土田光範",
                    department="HR",
                    email="tsuchida@company.example.com",
                    password_hash=PWD_CONTEXT.hash(admin_seed_password()),
                    active=True,
                    role_admin=True,
                    role_manager=False,
                    role_employee=False,
                    manager_emp_no=None,
                    manager_email=None,
                    must_change_password=True,
                    password_updated_at=None,
                    last_login_at=None,
                )
            )
        db.commit()


def seed_how_questions_if_needed() -> None:
    templates: Dict[str, List[str]] = {
        "innovation_courage": [
            "新しいアイデアや改善案を自ら提案したか",
            "リスクを理解した上で挑戦的な選択をしたか",
            "失敗から学び、次の行動に活かしたか",
            "現状に疑問を持ち、変える提案をしたか",
            "意思決定で必要な対立を恐れず発言したか",
        ],
        "agility_performance": [
            "優先順位を適切に切り替えられたか",
            "期限と品質のバランスを取り成果を出したか",
            "不確実な状況でも前に進めたか",
            "課題に素早く着手し改善を回したか",
            "パフォーマンス指標を意識して行動したか",
        ],
        "inclusion_collaboration": [
            "多様な意見を引き出し尊重したか",
            "チームの合意形成に貢献したか",
            "他部門と協力して成果を出したか",
            "情報共有を積極的に行ったか",
            "衝突を建設的に解消できたか",
        ],
        "integrity": [
            "約束・締切・合意を守ったか",
            "不都合な事実も正直に共有したか",
            "コンプライアンス/ルールを遵守したか",
            "判断の根拠を説明できる行動をしたか",
            "利害よりも正しさを優先できたか",
        ],
        "customer_focus": [
            "顧客課題を理解するための行動をしたか",
            "顧客価値を意思決定の中心に置いたか",
            "顧客フィードバックを改善に反映したか",
            "顧客体験を高める工夫をしたか",
            "顧客の成功指標を意識したか",
        ],
        "impact_on_others": [
            "周囲が成果を出しやすい支援をしたか",
            "期待値調整や合意形成を適切に行ったか",
            "相手に合わせたコミュニケーションをしたか",
            "建設的なフィードバックを提供したか",
            "チームの士気や学習に良い影響を与えたか",
        ],
        "change_leadership": [
            "変化の必要性を説明し巻き込んだか",
            "抵抗や不安に配慮しながら推進したか",
            "新しいやり方を定着させる工夫をしたか",
            "関係者の利害を調整して前進させたか",
            "変革の成果を測定し改善したか",
        ],
        "efficiency": [
            "ムダを見つけ削減したか",
            "業務を標準化/自動化する工夫をしたか",
            "再利用できる資産（資料/テンプレ）を作ったか",
            "ボトルネックを特定し解消したか",
            "少ない工数で成果を出す設計をしたか",
        ],
    }

    with SessionLocal() as db:
        any_row = db.execute(select(HowQuestion).limit(1)).scalar_one_or_none()
        if any_row:
            return

        for cat_key, qs in templates.items():
            for i, text in enumerate(qs, start=1):
                db.add(
                    HowQuestion(
                        category_key=cat_key,
                        category_label=cat_key,
                        question_no=i,
                        question_text=text,
                        active=True,
                    )
                )
        db.commit()


# ----------------------------
# ORM attach helper (Streamlit rerun safe)
# ----------------------------
def attach(db, obj):
    stt = inspect(obj)
    if stt.session is db:
        return obj
    return db.merge(obj)


# ----------------------------
# Auth / Session
# ----------------------------
@dataclass(frozen=True)
class AuthUser:
    emp_no: str
    name: str
    role: Role
    role_key: str


def set_page(page: str) -> None:
    st.session_state["page"] = page


def get_page() -> str:
    return st.session_state.get("page", "login")


def set_auth(user: Optional[AuthUser]) -> None:
    st.session_state["auth_user"] = asdict(user) if user else None


def get_auth() -> Optional[AuthUser]:
    raw = st.session_state.get("auth_user")
    return AuthUser(**raw) if raw else None


def logout() -> None:
    clear_selected_year()
    set_auth(None)
    set_page("login")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return PWD_CONTEXT.verify(plain, hashed)
    except Exception:
        return False


def require_login() -> AuthUser:
    user = get_auth()
    if not user:
        set_page("login")
        st.stop()

    if get_page() != "password_change":
        with SessionLocal() as db:
            emp = db.execute(select(Employee).where(Employee.emp_no == user.emp_no)).scalar_one_or_none()
            if not emp or not emp.active:
                logout()
                st.stop()
            if emp.must_change_password:
                set_page("password_change")
                st.stop()
    return user


def require_role(*allowed: Role) -> AuthUser:
    user = require_login()
    if user.role not in allowed:
        st.error("この画面にアクセスする権限がありません。")
        st.stop()
    return user


# ----------------------------
# Utility / Domain logic
# ----------------------------
def status_label_goal(status: str) -> str:
    return GOAL_STATUSES.get(status, status)


def status_label_eval(status: str) -> str:
    return EVAL_STATUSES.get(status, status)


def can_edit_goal(status: str) -> bool:
    return status in {"draft", "manager_returned", "hr_returned"}


def can_edit_eval_self(status: str) -> bool:
    return status in {"draft", "manager_returned", "hr_returned"}


def parse_iso_date(s: str) -> Optional[date]:
    try:
        if not s:
            return None
        return date.fromisoformat(str(s).strip())
    except Exception:
        return None


def to_iso_date(d: Optional[date]) -> str:
    return d.isoformat() if d else ""


def calc_what_from_business(items: List[GoalItem]) -> Tuple[float, str]:
    biz = [i for i in items if i.type == "business"]
    if not biz:
        return 0.0, "does_not_meet"
    total_w = sum(int(i.weight or 0) for i in biz)
    if total_w <= 0:
        return 0.0, "does_not_meet"
    weighted = 0.0
    for it in biz:
        weighted += float(it.weight or 0) * float(it.achieved_percent or 0)
    pct = weighted / total_w
    if pct >= WHAT_THRESHOLDS["exceeds"]:
        return pct, "exceeds"
    if pct >= WHAT_THRESHOLDS["meets"]:
        return pct, "meets"
    return pct, "does_not_meet"


def calc_how_from_scores(scores: List[int]) -> Tuple[int, float, str]:
    total = int(sum(scores))
    ratio = float(total / HOW_TOTAL_MAX) if HOW_TOTAL_MAX else 0.0
    if ratio >= HOW_EXCEEDS_RATIO:
        return total, ratio, "exceeds"
    if ratio >= HOW_MEETS_RATIO:
        return total, ratio, "meets"
    return total, ratio, "does_not_meet"


def category_averages(answers: List[HowAnswer]) -> Dict[str, float]:
    bucket: Dict[str, List[int]] = {}
    for a in answers:
        bucket.setdefault(a.category_key, []).append(int(a.score))
    return {k: (sum(v) / len(v) if v else 0.0) for k, v in bucket.items()}


def radar_chart(self_avg: Dict[str, float], mgr_avg: Dict[str, float]) -> None:
    labels = [label for _, label in HOW_CATEGORIES]
    keys = [k for k, _ in HOW_CATEGORIES]
    self_vals = [float(self_avg.get(k, 0.0)) for k in keys]
    mgr_vals = [float(mgr_avg.get(k, 0.0)) for k in keys]

    self_vals.append(self_vals[0])
    mgr_vals.append(mgr_vals[0])

    angles = [i / float(len(keys)) * 2.0 * 3.141592653589793 for i in range(len(keys))]
    angles.append(angles[0])

    fig = plt.figure()
    ax = plt.subplot(111, polar=True)
    ax.set_ylim(0, 4)
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(labels)
    ax.plot(angles, self_vals, linewidth=2, label="自己")
    ax.fill(angles, self_vals, alpha=0.15)
    ax.plot(angles, mgr_vals, linewidth=2, label="上長")
    ax.fill(angles, mgr_vals, alpha=0.15)
    ax.legend(loc="upper right", bbox_to_anchor=(1.2, 1.1))
    st.pyplot(fig, clear_figure=True)


def to_csv(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return ""
    headers = list(rows[0].keys())
    sio = StringIO()
    sio.write(",".join(headers) + "\n")

    def esc(x: Any) -> str:
        s = "" if x is None else str(x)
        if any(c in s for c in [",", "\n", '"']):
            s = '"' + s.replace('"', '""') + '"'
        return s

    for r in rows:
        sio.write(",".join(esc(r.get(h, "")) for h in headers) + "\n")
    return sio.getvalue()


def ensure_goal_exists_for_eval(db, employee_emp_no: str, year: int) -> Goal:
    goal = db.execute(select(Goal).where(Goal.employee_emp_no == employee_emp_no, Goal.year == year)).scalar_one_or_none()
    if not goal:
        raise ValueError("先に目標を作成してください。")
    if goal.status != "hr_approved":
        raise ValueError("評価を開始するには、目標がHR確認（公開）されている必要があります。")
    return goal


def get_or_create_evaluation(db, employee_emp_no: str, year: int, goal_id: int) -> Evaluation:
    ev = db.execute(select(Evaluation).where(Evaluation.employee_emp_no == employee_emp_no, Evaluation.year == year)).scalar_one_or_none()
    if ev:
        return ev
    ev = Evaluation(employee_emp_no=employee_emp_no, year=year, goal_id=goal_id, status="draft")
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


# ----------------------------
# UI Helpers
# ----------------------------
SMART_TEXT = """
### SMART理論（目標設定のフレームワーク）
- **S: Specific（具体的）**  
  何を・どこで・誰が・どれくらい、が説明できる状態にする
- **M: Measurable（測定可能）**  
  成果を数字・指標・判定条件で確認できるようにする
- **A: Achievable（達成可能）**  
  実行計画（任意）として「どうやるか」を書くとブレが減る
- **R: Relevant（関連性）**  
  部署ゴールや組織の方向性とつながっていることを示す
- **T: Time-bound（期限）**  
  いつまでに達成するか（日付）を明確にする
"""


def apply_custom_styles() -> None:
    st.markdown(
        f"""
<style>
:root {{
  --primary-color: {BRAND_COLORS['primary']};
  --secondary-color: {BRAND_COLORS['secondary']};
}}
.main-header {{
  background: linear-gradient(135deg, {BRAND_COLORS['primary']} 0%, {BRAND_COLORS['secondary']} 100%);
  padding: 20px;
  border-radius: 10px;
  color: {BRAND_COLORS['secondary']};
  margin-bottom: 20px;
  box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
}}
.main-header h1 {{
  color: {BRAND_COLORS['secondary']};
  margin: 0;
  font-size: 28px;
  font-weight: 700;
}}
.user-info {{
  color: {BRAND_COLORS['dark_gray']};
  font-size: 14px;
  text-align: right;
}}
[data-testid="stSidebar"] {{
  background-color: {BRAND_COLORS['light_gray']};
}}
.stTextInput > div > div > input,
.stSelectbox > div > div > select,
.stNumberInput > div > div > input {{
  border: 2px solid {BRAND_COLORS['border_gray']};
  border-radius: 5px;
}}
h2, h3 {{
  color: {BRAND_COLORS['secondary']};
  border-bottom: 3px solid {BRAND_COLORS['primary']};
  padding-bottom: 10px;
}}
.stButton > button {{
  background-color: {BRAND_COLORS['primary']};
  color: {BRAND_COLORS['secondary']};
  border: none;
  border-radius: 5px;
  font-weight: 600;
  padding: 10px 20px;
  transition: all 0.3s ease;
}}
.stButton > button:hover {{
  background-color: {BRAND_COLORS['secondary']};
  color: {BRAND_COLORS['primary']};
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
}}
</style>
""",
        unsafe_allow_html=True,
    )


def header_bar(user: Optional[AuthUser]) -> None:
    st.markdown(
        '<div class="main-header">'
        "<h1>🏭 Goal Management System</h1>"
        "<p style='margin: 5px 0 0 0; font-size: 12px;'>Stanley Black & Decker</p>"
        "</div>",
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        st.markdown("")
    with col2:
        st.markdown("")
    with col3:
        if user:
            st.markdown(
                f'<div class="user-info">👤 {user.name}<br><small>{user.role}</small><br>'
                f"<small>年度: {get_selected_year()}</small></div>",
                unsafe_allow_html=True,
            )
            if st.button("🚪 Logout", use_container_width=True):
                logout()
                st.rerun()


def nav_sidebar(user: Optional[AuthUser]) -> None:
    with st.sidebar:
        st.markdown(
            f'<div style="background-color: {BRAND_COLORS["primary"]}; padding: 15px; border-radius: 5px; margin-bottom: 10px;">'
            f'<h2 style="color: #000; margin: 0; font-size: 18px;">📊 Navigation</h2>'
            f"</div>",
            unsafe_allow_html=True,
        )

        st.markdown(SMART_TEXT)

        st.markdown("---")

        if not user:
            st.warning("⚠️ Please log in to access the system.")
            return

        if get_page() == "password_change":
            st.info("🔐 Password change required before continuing.")
            return

        st.markdown(f"**年度（ログイン時指定）:** {get_selected_year()}")
        st.button("🏠 Home", on_click=set_page, args=("home",), use_container_width=True)
        st.markdown("---")

        if user.role == "入力者":
            st.markdown("**Employee Functions**")
            st.button("📝 Goal Input", on_click=set_page, args=("goal_input",), use_container_width=True)
            st.button("👁️ View Goals", on_click=set_page, args=("goal_view_self",), use_container_width=True)
            st.button("⭐ Self Evaluation", on_click=set_page, args=("eval_input_self",), use_container_width=True)
            st.button("📋 View Evaluations", on_click=set_page, args=("eval_view_self",), use_container_width=True)
            st.button("🗣️ 1:1 Review", on_click=set_page, args=("oneonone_employee",), use_container_width=True)
            st.button("📊 Approval Status", on_click=set_page, args=("approval_status_self",), use_container_width=True)

        if user.role == "評価者":
            st.markdown("**Manager Functions**")
            st.button("📂 View Team Goals", on_click=set_page, args=("goal_view_manager",), use_container_width=True)
            st.button("✅ Approve Goals", on_click=set_page, args=("goal_approve_manager",), use_container_width=True)
            st.button("⭐ Evaluate Team", on_click=set_page, args=("eval_input_manager",), use_container_width=True)
            st.button("📅 Schedule 1:1", on_click=set_page, args=("oneonone_manager",), use_container_width=True)

        if user.role == "HR管理者":
            st.markdown("**HR Admin Functions**")
            st.button("📈 HR Dashboard", on_click=set_page, args=("hr_dashboard",), use_container_width=True)
            st.button("👥 Employee Master", on_click=set_page, args=("admin_employee_master",), use_container_width=True)
            st.button("✔️ Confirm Goals (HR)", on_click=set_page, args=("goal_approve_hr",), use_container_width=True)
            st.button("✔️ Confirm Evaluations (HR)", on_click=set_page, args=("eval_approve_hr",), use_container_width=True)
            st.button("📥 Export CSV", on_click=set_page, args=("admin_csv",), use_container_width=True)

        st.markdown("---")
        col1, col2 = st.columns(2)
        with col1:
            st.button("🔐 Password", on_click=set_page, args=("password_change",), use_container_width=True)
        with col2:
            if st.button("🚪 Logout", use_container_width=True):
                logout()
                st.rerun()


# ----------------------------
# Pages: Auth
# ----------------------------
def page_login() -> None:
    st.markdown(
        f"""
<div style="display: flex; justify-content: center; align-items: center; min-height: 60vh;">
  <div style="background: white; padding: 40px; border-radius: 10px;
              box-shadow: 0 4px 6px rgba(0,0,0,0.1); max-width: 420px; width: 100%;">
    <div style="text-align: center; margin-bottom: 30px;">
      <h1 style="color: {BRAND_COLORS['secondary']}; margin: 0 0 10px 0;">🏭</h1>
      <h2 style="color: {BRAND_COLORS['secondary']}; margin: 0 0 5px 0; font-size: 24px;">Stanley Black & Decker</h2>
      <p style="color: {BRAND_COLORS['dark_gray']}; margin: 0; font-size: 14px;">Goal Management System</p>
    </div>
""",
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        with st.form("login_form", clear_on_submit=False):
            role: Role = st.selectbox("📋 Select Role:", ["HR管理者", "評価者", "入力者"])
            login_year = year_input_login(datetime.utcnow().year)
            emp_no = st.text_input("👤 Employee ID:", placeholder="Example: 000001")
            password = st.text_input("🔐 Password:", type="password")
            submitted = st.form_submit_button("🔓 Sign In", use_container_width=True)

        st.divider()
        if st.button("❓ Forgot Password?", use_container_width=True):
            set_page("forgot_password")

    st.markdown("</div></div>", unsafe_allow_html=True)

    if not submitted:
        return

    emp_no = emp_no.strip()
    if not emp_no or not password:
        st.error("❌ Please enter Employee ID and Password.")
        return

    with SessionLocal() as db:
        emp = db.execute(select(Employee).where(Employee.emp_no == emp_no, Employee.active.is_(True))).scalar_one_or_none()
        if not emp or not verify_password(password, emp.password_hash):
            st.error("❌ Invalid Employee ID or Password.")
            return

        role_flag = ROLE_TO_FLAG[role]
        if not bool(getattr(emp, role_flag)):
            st.error("❌ You do not have permission for this role.")
            return

        emp.last_login_at = datetime.utcnow()
        db.add(emp)
        db.commit()

        set_selected_year(int(login_year), force=True)

        auth = AuthUser(emp_no=emp.emp_no, name=emp.name, role=role, role_key=ROLE_TO_KEY[role])
        set_auth(auth)

        if emp.must_change_password:
            set_page("password_change")
            st.warning("🔐 Password change required on first login.")
        else:
            set_page("home")
            st.success("✅ Welcome back!")
        st.rerun()


def page_forgot_password() -> None:
    st.subheader("Password Recovery")
    st.info("👨‍💼 Contact your administrator to reset your password.")
    if st.button("🔙 Back to Login"):
        set_page("login")
        st.rerun()


def page_password_change() -> None:
    user = get_auth()
    if not user:
        set_page("login")
        st.stop()

    st.subheader("Change Password")

    with st.form("pw_change", clear_on_submit=True):
        current = st.text_input("現在のパスワード", type="password")
        new1 = st.text_input("新しいパスワード", type="password")
        new2 = st.text_input("新しいパスワード（確認）", type="password")
        ok = st.form_submit_button("変更する")

    if not ok:
        st.caption("要件：8文字以上")
        return

    if not current or not new1 or not new2:
        st.error("全て入力してください。")
        return
    if new1 != new2:
        st.error("新しいパスワードが一致しません。")
        return
    if len(new1) < 8:
        st.error("新しいパスワードは8文字以上にしてください。")
        return

    with SessionLocal() as db:
        emp = db.execute(select(Employee).where(Employee.emp_no == user.emp_no)).scalar_one_or_none()
        if not emp or not emp.active:
            logout()
            st.stop()

        if not verify_password(current, emp.password_hash):
            st.error("現在のパスワードが違います。")
            return

        emp.password_hash = PWD_CONTEXT.hash(new1)
        emp.must_change_password = False
        emp.password_updated_at = datetime.utcnow()
        db.add(emp)
        db.commit()

    st.success("パスワードを変更しました。")
    set_page("home")
    st.rerun()


# ----------------------------
# Pages: Home
# ----------------------------
def page_home() -> None:
    user = require_login()
    year = get_selected_year()

    st.markdown(
        f"""
<div style="background: linear-gradient(135deg, {BRAND_COLORS['primary']}, {BRAND_COLORS['secondary']});
            padding: 30px; border-radius: 10px; margin-bottom: 30px; color: white;">
  <h1 style="margin: 0 0 10px 0; color: {BRAND_COLORS['secondary']};"> Welcome, {user.name}!</h1>
  <p style="margin: 0; color: {BRAND_COLORS['dark_gray']};">Select an action below to get started.</p>
  <p style="margin: 8px 0 0 0; color: {BRAND_COLORS['dark_gray']};"><b>年度:</b> {year}</p>
</div>
""",
        unsafe_allow_html=True,
    )

    if user.role == "入力者":
        st.markdown("### 📝 Your Tasks")
        c1, c2 = st.columns(2)
        with c1:
            st.button("📝 Input Goals", use_container_width=True, on_click=set_page, args=("goal_input",))
            st.button("⭐ Self Evaluation", use_container_width=True, on_click=set_page, args=("eval_input_self",))
            st.button("📊 Status Check", use_container_width=True, on_click=set_page, args=("approval_status_self",))
        with c2:
            st.button("👁️ View Goals", use_container_width=True, on_click=set_page, args=("goal_view_self",))
            st.button("📋 View Evaluations", use_container_width=True, on_click=set_page, args=("eval_view_self",))
            st.button("🗣️ 1:1 Review", use_container_width=True, on_click=set_page, args=("oneonone_employee",))

    if user.role == "評価者":
        st.markdown("### 👥 Team Management")
        c1, c2 = st.columns(2)
        with c1:
            st.button("📂 View Team Goals", use_container_width=True, on_click=set_page, args=("goal_view_manager",))
            st.button("⭐ Evaluate Team", use_container_width=True, on_click=set_page, args=("eval_input_manager",))
        with c2:
            st.button("✅ Approve Goals", use_container_width=True, on_click=set_page, args=("goal_approve_manager",))
            st.button("📅 Schedule 1:1", use_container_width=True, on_click=set_page, args=("oneonone_manager",))

    if user.role == "HR管理者":
        st.markdown("### ⚙️ HR Administration")
        c1, c2, c3 = st.columns(3)
        with c1:
            st.button("📈 HR Dashboard", use_container_width=True, on_click=set_page, args=("hr_dashboard",))
        with c2:
            st.button("👥 Employee Master", use_container_width=True, on_click=set_page, args=("admin_employee_master",))
        with c3:
            st.button("📥 Export CSV", use_container_width=True, on_click=set_page, args=("admin_csv",))
        c4, c5 = st.columns(2)
        with c4:
            st.button("✔️ Approve Goals (HR)", use_container_width=True, on_click=set_page, args=("goal_approve_hr",))
        with c5:
            st.button("✔️ Approve Evaluations (HR)", use_container_width=True, on_click=set_page, args=("eval_approve_hr",))


# ----------------------------
# Admin: Employee Master + Reset + CSV Upload
# ----------------------------
def _generate_temp_password() -> str:
    return secrets.token_urlsafe(10)


def _parse_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in {"1", "true", "t", "yes", "y", "on", "○"}:
        return True
    if s in {"0", "false", "f", "no", "n", "off", "×"}:
        return False
    return default


def _csv_template_text() -> str:
    return (
        "emp_no,name,email,department,active,role_admin,role_manager,role_employee,manager_emp_no,password\n"
        "000002,山田太郎,yamada@example.com,Sales,1,0,1,1,010000,ChangeMe_1234\n"
        "010000,佐藤花子,sato@example.com,Sales,1,0,1,0,,ChangeMe_1234\n"
    )


def _decode_csv_bytes(b: bytes) -> str:
    # UTF-8(BOM含む) → CP932 → UTF-8(緩め) の順に試す
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    # 最後の手段：置換
    return b.decode("cp932", errors="replace")


def page_admin_employee_master() -> None:
    require_role("HR管理者")
    st.subheader("Employee Master Management")

    st.markdown("### CSV Upload")
    st.caption(
        "列: emp_no,name,email,department,active,role_admin,role_manager,role_employee,manager_emp_no,password\n"
        "※ active/role_* は 1/0 推奨（○/× でも可）\n"
        "※ password が空なら ChangeMe_1234 を設定（初回ログイン時変更は必須）"
    )
    st.download_button(
        "CSVテンプレートをダウンロード",
        data=_csv_template_text().encode("utf-8"),
        file_name="employee_master_template.csv",
        mime="text/csv",
    )

    up = st.file_uploader("従業員マスタCSVをアップロード", type=["csv"])
    if up is not None:
        raw = _decode_csv_bytes(up.getvalue())
        reader = csv.DictReader(StringIO(raw))
        required_cols = {"emp_no", "name"}
        missing = [c for c in required_cols if c not in (reader.fieldnames or [])]
        if missing:
            st.error(f"必須列が足りません: {missing}")
        else:
            updated_count = 0
            created_count = 0
            with SessionLocal() as db:
                for row in reader:
                    emp_no = str(row.get("emp_no", "")).strip()
                    name = str(row.get("name", "")).strip()
                    if not emp_no or not name:
                        continue

                    email = str(row.get("email", "") or "").strip() or None
                    department = str(row.get("department", "") or "").strip()
                    active = _parse_bool(row.get("active", "1"), True)
                    role_admin = _parse_bool(row.get("role_admin", "0"), False)
                    role_manager = _parse_bool(row.get("role_manager", "0"), False)
                    role_employee = _parse_bool(row.get("role_employee", "1"), True)
                    manager_emp_no = str(row.get("manager_emp_no", "") or "").strip() or None

                    password = str(row.get("password", "") or "").strip()
                    if not password:
                        password = DEFAULT_PASSWORD_IF_BLANK

                    existing = db.execute(select(Employee).where(Employee.emp_no == emp_no)).scalar_one_or_none()
                    if existing:
                        existing.name = name
                        existing.email = email
                        existing.department = department
                        existing.active = active
                        existing.role_admin = role_admin
                        existing.role_manager = role_manager
                        existing.role_employee = role_employee
                        existing.manager_emp_no = manager_emp_no
                        existing.password_hash = PWD_CONTEXT.hash(password)
                        existing.must_change_password = True
                        existing.password_updated_at = None
                        db.add(existing)
                        updated_count += 1
                    else:
                        db.add(
                            Employee(
                                emp_no=emp_no,
                                name=name,
                                email=email,
                                department=department,
                                password_hash=PWD_CONTEXT.hash(password),
                                active=active,
                                role_admin=role_admin,
                                role_manager=role_manager,
                                role_employee=role_employee,
                                manager_emp_no=manager_emp_no,
                                manager_email=None,
                                must_change_password=True,
                                password_updated_at=None,
                                last_login_at=None,
                            )
                        )
                        created_count += 1

                db.commit()

            st.success(f"CSV取り込み完了：新規 {created_count} / 更新 {updated_count}")

    st.markdown("---")
    st.markdown("### Add/Update Manually")

    with st.form("emp_upsert", clear_on_submit=True):
        emp_no = st.text_input("従業員番号", placeholder="例: 000002")
        name = st.text_input("氏名", placeholder="例: 山田 太郎")
        email = st.text_input("メールアドレス（任意）", placeholder="例: yamada@example.com")
        department = st.text_input("部署", placeholder="例: Sales")
        password = st.text_input("パスワード（空なら ChangeMe_1234 を設定）", type="password")
        active = st.checkbox("在籍", value=True)

        col1, col2, col3 = st.columns(3)
        with col1:
            role_admin = st.checkbox("管理者権限")
        with col2:
            role_manager = st.checkbox("評価者権限")
        with col3:
            role_employee = st.checkbox("入力者権限", value=True)

        manager_emp_no = st.text_input("上長の従業員番号（任意）", placeholder="例: 010000（評価者）")
        ok = st.form_submit_button("保存")

    if ok:
        emp_no = emp_no.strip()
        if not emp_no or not name.strip():
            st.error("従業員番号と氏名は必須です。")
        elif not (role_admin or role_manager or role_employee):
            st.error("少なくとも1つの権限を付与してください。")
        else:
            pw = password.strip() if password else ""
            if not pw:
                pw = DEFAULT_PASSWORD_IF_BLANK

            with SessionLocal() as db:
                existing = db.execute(select(Employee).where(Employee.emp_no == emp_no)).scalar_one_or_none()
                if existing:
                    existing.name = name.strip()
                    existing.email = email.strip() or None
                    existing.department = department.strip()
                    existing.active = bool(active)
                    existing.role_admin = bool(role_admin)
                    existing.role_manager = bool(role_manager)
                    existing.role_employee = bool(role_employee)
                    existing.manager_emp_no = manager_emp_no.strip() or None
                    existing.password_hash = PWD_CONTEXT.hash(pw)
                    existing.must_change_password = True
                    existing.password_updated_at = None
                    db.add(existing)
                    db.commit()
                    st.success("更新しました。")
                else:
                    db.add(
                        Employee(
                            emp_no=emp_no,
                            name=name.strip(),
                            email=email.strip() or None,
                            department=department.strip(),
                            password_hash=PWD_CONTEXT.hash(pw),
                            active=bool(active),
                            role_admin=bool(role_admin),
                            role_manager=bool(role_manager),
                            role_employee=bool(role_employee),
                            manager_emp_no=manager_emp_no.strip() or None,
                            manager_email=None,
                            must_change_password=True,
                            password_updated_at=None,
                            last_login_at=None,
                        )
                    )
                    db.commit()
                    st.success("追加しました。")

    st.markdown("---")
    st.write("従業員一覧 / リセット")

    with SessionLocal() as db:
        emps = db.execute(select(Employee).order_by(Employee.emp_no.asc())).scalars().all()
        if not emps:
            st.warning("従業員がいません。")
            return

        options = {f"{e.emp_no} {e.name}（{e.department}）": e.emp_no for e in emps}
        selected_label = st.selectbox("リセット対象を選択", list(options.keys()))
        selected_emp_no = options[selected_label]

        colr1, colr2 = st.columns([1, 2])
        with colr1:
            do_reset = st.button("一時パスワードを発行（リセット）")
        with colr2:
            st.caption("発行後、ユーザーは次回ログインで必ずPW変更します。")

        if do_reset:
            temp_pw = _generate_temp_password()
            emp = db.execute(select(Employee).where(Employee.emp_no == selected_emp_no)).scalar_one()
            emp.password_hash = PWD_CONTEXT.hash(temp_pw)
            emp.must_change_password = True
            emp.password_updated_at = None
            db.add(emp)
            db.commit()
            st.warning("一時パスワード（必ず控えてください）")
            st.code(temp_pw, language="text")

        rows = []
        for e in emps:
            rows.append(
                {
                    "従業員番号": e.emp_no,
                    "氏名": e.name,
                    "メール": e.email or "",
                    "部署": e.department,
                    "在籍": "○" if e.active else "×",
                    "管理者": "○" if e.role_admin else "",
                    "評価者": "○" if e.role_manager else "",
                    "入力者": "○" if e.role_employee else "",
                    "上長番号": e.manager_emp_no or "",
                    "初回変更必須": "○" if e.must_change_password else "",
                    "最終ログイン": e.last_login_at.strftime("%Y-%m-%d %H:%M") if e.last_login_at else "",
                    "更新日": e.updated_at.strftime("%Y-%m-%d %H:%M"),
                }
            )
        st.dataframe(rows, use_container_width=True)


# ---- Part 2 continues ----

# ---- Part 2 ----
# Goals (Employee) - New UI (business/dev) + date inputs + max limits

def load_or_create_goal(db, emp_no: str, year: int) -> Goal:
    goal = db.execute(select(Goal).where(Goal.employee_emp_no == emp_no, Goal.year == year)).scalar_one_or_none()
    if goal:
        _ = goal.items
        _ = goal.approvals
        return goal
    goal = Goal(employee_emp_no=emp_no, year=year, status="draft")
    db.add(goal)
    db.commit()
    db.refresh(goal)
    return goal


def _validate_business_items(items: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    errs: List[str] = []

    filled = [r for r in items if any(str(r.get(k, "")).strip() for k in ["対象業務", "達成したい結果", "期限", "部署ゴールとの関連性", "実行計画"])]
    if not filled:
        return True, []  # businessは0件でもOK（devがあるかは全体で判定）

    # 必須（実行計画のみ任意）
    for i, r in enumerate(filled, start=1):
        if not str(r.get("対象業務", "")).strip():
            errs.append(f"Business #{i}: 「今回の対象となる業務」は必須です。")
        if not str(r.get("達成したい結果", "")).strip():
            errs.append(f"Business #{i}: 「達成したい結果」は必須です。")
        if not isinstance(r.get("期限"), date):
            errs.append(f"Business #{i}: 「期限」は日付で指定してください。")
        if not str(r.get("部署ゴールとの関連性", "")).strip():
            errs.append(f"Business #{i}: 「部署ゴールとの関連性」は必須です。")

        # Weightは必要（what計算に使用）
        try:
            w = int(r.get("Weight", 0))
            if w < 0 or w > 100:
                errs.append(f"Business #{i}: Weight は 0〜100 です。")
        except Exception:
            errs.append(f"Business #{i}: Weight は数値で入力してください。")

    wsum = 0
    for r in filled:
        try:
            wsum += int(r.get("Weight", 0))
        except Exception:
            pass
    if filled and wsum != 100:
        errs.append(f"Business: Weight 合計が 100 ではありません（現在: {wsum}）。")

    return (len(errs) == 0), errs


def _validate_dev_items(items: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    errs: List[str] = []
    filled = [r for r in items if any(str(r.get(k, "")).strip() for k in ["なりたい人物像/身につけたいスキル", "現在の自分とのギャップ", "どのような行動を行いますか？", "実行計画", "完了時期"])]
    if not filled:
        return True, []

    for i, r in enumerate(filled, start=1):
        if not str(r.get("なりたい人物像/身につけたいスキル", "")).strip():
            errs.append(f"Development #{i}: 「なりたい人物像/身につけたいスキル」は必須です。")
        if not str(r.get("現在の自分とのギャップ", "")).strip():
            errs.append(f"Development #{i}: 「現在の自分とのギャップ」は必須です。")
        if not str(r.get("どのような行動を行いますか？", "")).strip():
            errs.append(f"Development #{i}: 「どのような行動を行いますか？」は必須です。")
        # 実行計画/完了時期は任意（完了時期は date or None）

    return (len(errs) == 0), errs


def _initial_business_rows_from_goal(goal: Goal) -> List[Dict[str, Any]]:
    biz = [it for it in goal.items if it.type == "business"]
    rows: List[Dict[str, Any]] = []
    for it in biz[:BUSINESS_GOAL_MAX]:
        rows.append(
            {
                "対象業務": it.specific,
                "達成したい結果": it.measurable,
                "期限": parse_iso_date(it.time_bound) or date.today(),
                "部署ゴールとの関連性": it.relevant,
                "実行計画": it.achievable or "",
                "Weight": int(it.weight or 0),
            }
        )
    while len(rows) < BUSINESS_GOAL_MAX:
        rows.append(
            {
                "対象業務": "",
                "達成したい結果": "",
                "期限": date.today(),
                "部署ゴールとの関連性": "",
                "実行計画": "",
                "Weight": 0 if rows else 100,  # 1件目は100を初期値
            }
        )
    return rows


def _initial_dev_rows_from_goal(goal: Goal) -> List[Dict[str, Any]]:
    dev = [it for it in goal.items if it.type == "development"]
    rows: List[Dict[str, Any]] = []
    for it in dev[:DEVELOPMENT_GOAL_MAX]:
        rows.append(
            {
                "なりたい人物像/身につけたいスキル": it.specific,
                "現在の自分とのギャップ": it.measurable,
                "どのような行動を行いますか？": it.relevant,
                "実行計画": it.achievable or "",
                "完了時期": parse_iso_date(it.time_bound),
            }
        )
    while len(rows) < DEVELOPMENT_GOAL_MAX:
        rows.append(
            {
                "なりたい人物像/身につけたいスキル": "",
                "現在の自分とのギャップ": "",
                "どのような行動を行いますか？": "",
                "実行計画": "",
                "完了時期": None,
            }
        )
    return rows


def page_goal_input() -> None:
    user = require_role("入力者")
    year = get_selected_year()

    st.subheader("目標入力（新フォーマット）")
    st.caption(f"年度: {year} / business最大{BUSINESS_GOAL_MAX}件・development最大{DEVELOPMENT_GOAL_MAX}件")

    with SessionLocal() as db:
        emp = db.execute(select(Employee).where(Employee.emp_no == user.emp_no)).scalar_one()
        goal = load_or_create_goal(db, user.emp_no, year)
        st.caption(f"状態: {status_label_goal(goal.status)}")
        editable = can_edit_goal(goal.status)

        if not emp.manager_emp_no:
            st.warning("従業員マスタで上長が設定されていません。Submitできません。")

        state_key = f"goalui:{user.emp_no}:{year}"
        if state_key not in st.session_state:
            st.session_state[state_key] = {
                "biz": _initial_business_rows_from_goal(goal),
                "dev": _initial_dev_rows_from_goal(goal),
            }

        st.markdown("## ① Business goals（評価対象）")
        st.caption("必須: 対象業務 / 達成したい結果 / 期限(日付) / 部署ゴールとの関連性　任意: 実行計画")

        biz_rows: List[Dict[str, Any]] = st.session_state[state_key]["biz"]
        for i in range(BUSINESS_GOAL_MAX):
            with st.expander(f"Business #{i+1}", expanded=(i == 0)):
                biz_rows[i]["対象業務"] = st.text_area(
                    "① 今回の対象となる業務",
                    value=biz_rows[i].get("対象業務", ""),
                    disabled=not editable,
                    key=f"biz_target_{state_key}_{i}",
                )
                biz_rows[i]["達成したい結果"] = st.text_area(
                    "② 達成したい結果",
                    value=biz_rows[i].get("達成したい結果", ""),
                    disabled=not editable,
                    key=f"biz_result_{state_key}_{i}",
                )
                biz_rows[i]["期限"] = st.date_input(
                    "③ 期限",
                    value=biz_rows[i].get("期限") or date.today(),
                    disabled=not editable,
                    key=f"biz_deadline_{state_key}_{i}",
                )
                biz_rows[i]["部署ゴールとの関連性"] = st.text_area(
                    "④ 部署ゴールとの関連性",
                    value=biz_rows[i].get("部署ゴールとの関連性", ""),
                    disabled=not editable,
                    key=f"biz_align_{state_key}_{i}",
                )
                biz_rows[i]["実行計画"] = st.text_area(
                    "実行計画（任意）",
                    value=biz_rows[i].get("実行計画", ""),
                    disabled=not editable,
                    key=f"biz_plan_{state_key}_{i}",
                )
                biz_rows[i]["Weight"] = st.number_input(
                    "Weight（合計100）",
                    min_value=0,
                    max_value=100,
                    value=int(biz_rows[i].get("Weight") or 0),
                    step=1,
                    disabled=not editable,
                    key=f"biz_weight_{state_key}_{i}",
                )

        st.markdown("## ② Development goals（キャリア）")
        st.caption("必須: なりたい人物像/スキル / ギャップ / 行動　任意: 実行計画 / 完了時期(日付)")
        dev_rows: List[Dict[str, Any]] = st.session_state[state_key]["dev"]
        for i in range(DEVELOPMENT_GOAL_MAX):
            with st.expander(f"Development #{i+1}", expanded=(i == 0)):
                dev_rows[i]["なりたい人物像/身につけたいスキル"] = st.text_area(
                    "① なりたい人物像/身につけたいスキル",
                    value=dev_rows[i].get("なりたい人物像/身につけたいスキル", ""),
                    disabled=not editable,
                    key=f"dev_vision_{state_key}_{i}",
                )
                dev_rows[i]["現在の自分とのギャップ"] = st.text_area(
                    "② 現在の自分とのギャップ",
                    value=dev_rows[i].get("現在の自分とのギャップ", ""),
                    disabled=not editable,
                    key=f"dev_gap_{state_key}_{i}",
                )
                dev_rows[i]["どのような行動を行いますか？"] = st.text_area(
                    "③ どのような行動を行いますか？",
                    value=dev_rows[i].get("どのような行動を行いますか？", ""),
                    disabled=not editable,
                    key=f"dev_action_{state_key}_{i}",
                )
                dev_rows[i]["実行計画"] = st.text_area(
                    "実行計画（任意）",
                    value=dev_rows[i].get("実行計画", ""),
                    disabled=not editable,
                    key=f"dev_plan_{state_key}_{i}",
                )
                dev_rows[i]["完了時期"] = st.date_input(
                    "完了時期（任意）",
                    value=dev_rows[i].get("完了時期") or date.today(),
                    disabled=not editable,
                    key=f"dev_done_{state_key}_{i}",
                )
                # 任意にしたいので、未入力相当を作るチェック
                dev_rows[i]["完了時期_任意"] = st.checkbox(
                    "完了時期は未設定（任意）",
                    value=(dev_rows[i].get("完了時期") is None),
                    disabled=not editable,
                    key=f"dev_done_none_{state_key}_{i}",
                )
                if dev_rows[i]["完了時期_任意"]:
                    dev_rows[i]["完了時期"] = None

        st.session_state[state_key]["biz"] = biz_rows
        st.session_state[state_key]["dev"] = dev_rows

        st.markdown("---")
        c1, c2, c3 = st.columns([1, 1, 2])
        with c1:
            save = st.button("保存（下書き）", disabled=not editable, use_container_width=True)
        with c2:
            submit = st.button("上長へ承認依頼（Submit）", disabled=not editable, use_container_width=True)
        with c3:
            st.caption("Submitすると「上長承認待ち」になります（差戻し時は再編集可）。")

        if save or submit:
            if submit and not emp.manager_emp_no:
                st.error("上長が設定されていないためSubmitできません。")
                st.stop()

            # validate
            ok1, e1 = _validate_business_items(biz_rows)
            ok2, e2 = _validate_dev_items(dev_rows)
            errs = e1 + e2

            # 全体で1件以上（biz or dev）
            any_biz = any(str(r.get("対象業務", "")).strip() for r in biz_rows)
            any_dev = any(str(r.get("なりたい人物像/身につけたいスキル", "")).strip() for r in dev_rows)
            if not (any_biz or any_dev):
                errs.append("Business または Development を1件以上入力してください。")

            if not (ok1 and ok2) or errs:
                st.error("入力に不備があります。")
                for msg in errs:
                    st.write(f"- {msg}")
                st.stop()

            goal = load_or_create_goal(db, user.emp_no, year)
            if not can_edit_goal(goal.status):
                st.error("この目標は編集できない状態です。")
                st.stop()

            # rerun/別セッション対策
            goal = attach(db, goal)

            # delete old items
            for it in list(goal.items):
                db.delete(it)
            db.flush()

            # insert business
            for r in biz_rows:
                if not any(str(r.get(k, "")).strip() for k in ["対象業務", "達成したい結果", "部署ゴールとの関連性", "実行計画"]) and not isinstance(r.get("期限"), date):
                    continue
                if not str(r.get("対象業務", "")).strip():
                    continue  # 空はスキップ
                db.add(
                    GoalItem(
                        goal_id=goal.id,
                        type="business",
                        specific=str(r.get("対象業務", "")).strip(),
                        measurable=str(r.get("達成したい結果", "")).strip(),
                        relevant=str(r.get("部署ゴールとの関連性", "")).strip(),
                        achievable=str(r.get("実行計画", "") or "").strip(),
                        time_bound=to_iso_date(r.get("期限")),
                        career_vision=None,
                        weight=int(r.get("Weight") or 0),
                        achieved_percent=int(0),
                    )
                )

            # insert development
            for r in dev_rows:
                if not str(r.get("なりたい人物像/身につけたいスキル", "")).strip():
                    continue
                done = r.get("完了時期")
                db.add(
                    GoalItem(
                        goal_id=goal.id,
                        type="development",
                        specific=str(r.get("なりたい人物像/身につけたいスキル", "")).strip(),
                        measurable=str(r.get("現在の自分とのギャップ", "")).strip(),
                        relevant=str(r.get("どのような行動を行いますか？", "")).strip(),
                        achievable=str(r.get("実行計画", "") or "").strip(),
                        time_bound=to_iso_date(done) if isinstance(done, date) else "",
                        career_vision=None,
                        weight=None,
                        achieved_percent=None,
                    )
                )

            if save:
                goal.status = "draft"
                db.add(GoalApproval(goal_id=goal.id, stage="employee", action="save", comment="", actor_emp_no=user.emp_no))
            if submit:
                goal.status = "submitted"
                db.add(GoalApproval(goal_id=goal.id, stage="employee", action="submit", comment="", actor_emp_no=user.emp_no))

            # ここが以前落ちた箇所：merge済みのgoalをaddする
            db.add(goal)
            db.commit()

            st.success("保存しました。" if save else "上長へ承認依頼しました。")

            if submit:
                emp_email = emp.email
                manager = db.execute(select(Employee).where(Employee.emp_no == emp.manager_emp_no)).scalar_one_or_none()
                manager_email = manager.email if manager else None

                if manager_email:
                    send_email(
                        manager_email,
                        f"【目標管理】{user.name}さんが目標を提出しました（{year}年度）",
                        f"こんにちは、\n\n{user.name}さんが{year}年度の目標を提出しました。\n確認・承認をお願いいたします。\n\n---\n本メールは自動送信されています。",
                    )
                if emp_email:
                    send_email(
                        emp_email,
                        f"【目標管理】目標を提出しました（{year}年度）",
                        f"こんにちは {user.name}さん,\n\nあなたが目標を上長に提出しました。\n承認をお待ちしています。\n\n---\n本メールは自動送信されています。",
                    )

            st.session_state.pop(state_key, None)
            st.rerun()


def page_goal_view_self() -> None:
    user = require_role("入力者")
    year = get_selected_year()

    st.subheader("目標閲覧（自分）")
    with SessionLocal() as db:
        goal = db.execute(select(Goal).where(Goal.employee_emp_no == user.emp_no, Goal.year == year)).scalar_one_or_none()
        if not goal:
            st.info("この年度の目標はまだありません。")
            return

        st.caption(f"年度: {year} / 状態: {status_label_goal(goal.status)}")
        items = db.execute(select(GoalItem).where(GoalItem.goal_id == goal.id).order_by(GoalItem.id.asc())).scalars().all()
        approvals = db.execute(select(GoalApproval).where(GoalApproval.goal_id == goal.id).order_by(GoalApproval.acted_at.asc())).scalars().all()

        biz = [it for it in items if it.type == "business"]
        dev = [it for it in items if it.type == "development"]

        st.markdown("### Business goals")
        if not biz:
            st.write("（なし）")
        for idx, it in enumerate(biz, start=1):
            with st.expander(f"Business #{idx}（Weight {it.weight}）"):
                st.write(f"**① 対象業務**: {it.specific}")
                st.write(f"**② 達成したい結果**: {it.measurable}")
                st.write(f"**③ 期限**: {it.time_bound}")
                st.write(f"**④ 部署ゴールとの関連性**: {it.relevant}")
                st.write(f"**実行計画（任意）**: {it.achievable}")

        st.markdown("### Development goals")
        if not dev:
            st.write("（なし）")
        for idx, it in enumerate(dev, start=1):
            with st.expander(f"Development #{idx}"):
                st.write(f"**① なりたい人物像/身につけたいスキル**: {it.specific}")
                st.write(f"**② 現在の自分とのギャップ**: {it.measurable}")
                st.write(f"**③ 行動**: {it.relevant}")
                st.write(f"**実行計画（任意）**: {it.achievable}")
                st.write(f"**完了時期（任意）**: {it.time_bound or '（未設定）'}")

        st.markdown("---")
        st.markdown("### 承認ログ")
        rows = [
            {
                "日時": a.acted_at.strftime("%Y-%m-%d %H:%M"),
                "ステージ": a.stage,
                "アクション": a.action,
                "実行者": a.actor_emp_no,
                "コメント": (a.comment or "").strip(),
            }
            for a in approvals
        ]
        st.dataframe(rows, use_container_width=True)


def page_goal_view_manager() -> None:
    user = require_role("評価者")
    year = get_selected_year()

    st.subheader("部下目標 閲覧（上長・閲覧専用）")
    with SessionLocal() as db:
        subs = db.execute(select(Employee).where(Employee.manager_emp_no == user.emp_no, Employee.active.is_(True))).scalars().all()
        if not subs:
            st.info("部下がいません。")
            return

        sub_options = {f"{e.emp_no} {e.name}（{e.department}）": e.emp_no for e in subs}
        target_label = st.selectbox("部下を選択", list(sub_options.keys()))
        emp_no = sub_options[target_label]

        goal = db.execute(select(Goal).where(Goal.employee_emp_no == emp_no, Goal.year == year)).scalar_one_or_none()
        if not goal:
            st.info("目標がありません。")
            return

        st.caption(f"年度: {year} / 状態: {status_label_goal(goal.status)}")
        items = db.execute(select(GoalItem).where(GoalItem.goal_id == goal.id).order_by(GoalItem.id.asc())).scalars().all()
        biz = [it for it in items if it.type == "business"]
        dev = [it for it in items if it.type == "development"]

        st.markdown("### Business goals")
        for idx, it in enumerate(biz, start=1):
            with st.expander(f"Business #{idx}（Weight {it.weight}）"):
                st.write(f"**① 対象業務**: {it.specific}")
                st.write(f"**② 達成したい結果**: {it.measurable}")
                st.write(f"**③ 期限**: {it.time_bound}")
                st.write(f"**④ 部署ゴールとの関連性**: {it.relevant}")
                st.write(f"**実行計画（任意）**: {it.achievable}")

        st.markdown("### Development goals")
        for idx, it in enumerate(dev, start=1):
            with st.expander(f"Development #{idx}"):
                st.write(f"**① なりたい人物像/身につけたいスキル**: {it.specific}")
                st.write(f"**② 現在の自分とのギャップ**: {it.measurable}")
                st.write(f"**③ 行動**: {it.relevant}")
                st.write(f"**実行計画（任意）**: {it.achievable}")
                st.write(f"**完了時期（任意）**: {it.time_bound or '（未設定）'}")


def page_goal_approve_manager() -> None:
    user = require_role("評価者")
    year = get_selected_year()
    st.subheader("部下目標 承認/差戻し（上長）")

    with SessionLocal() as db:
        subs = db.execute(select(Employee).where(Employee.manager_emp_no == user.emp_no, Employee.active.is_(True))).scalars().all()
        if not subs:
            st.info("部下がいません。")
            return

        sub_options = {f"{e.emp_no} {e.name}": e.emp_no for e in subs}
        target_label = st.selectbox("部下を選択", list(sub_options.keys()))
        emp_no = sub_options[target_label]

        goal = db.execute(select(Goal).where(Goal.employee_emp_no == emp_no, Goal.year == year)).scalar_one_or_none()
        if not goal:
            st.info("目標がありません。")
            return

        st.caption(f"年度: {year} / 状態: {status_label_goal(goal.status)}")
        items = db.execute(select(GoalItem).where(GoalItem.goal_id == goal.id).order_by(GoalItem.id.asc())).scalars().all()

        st.markdown("### 目標内容")
        for it in items:
            if it.type == "business":
                title = f"Business（Weight {it.weight}）"
                with st.expander(title):
                    st.write(f"**① 対象業務**: {it.specific}")
                    st.write(f"**② 達成したい結果**: {it.measurable}")
                    st.write(f"**③ 期限**: {it.time_bound}")
                    st.write(f"**④ 部署ゴールとの関連性**: {it.relevant}")
                    st.write(f"**実行計画（任意）**: {it.achievable}")
            else:
                with st.expander("Development"):
                    st.write(f"**① なりたい人物像/身につけたいスキル**: {it.specific}")
                    st.write(f"**② 現在の自分とのギャップ**: {it.measurable}")
                    st.write(f"**③ 行動**: {it.relevant}")
                    st.write(f"**実行計画（任意）**: {it.achievable}")
                    st.write(f"**完了時期（任意）**: {it.time_bound or '（未設定）'}")

        if goal.status != "submitted":
            st.info("この目標は上長承認待ちではありません。")
            return

        st.markdown("---")
        comment = st.text_area("コメント（差し戻し時は必須）", key=f"gm_comment_{emp_no}_{year}")
        c1, c2 = st.columns(2)
        with c1:
            approve = st.button("承認（HRへ提出）", use_container_width=True)
        with c2:
            ret = st.button("差し戻し", use_container_width=True)

        if approve:
            goal = attach(db, goal)
            goal.status = "manager_approved"
            db.add(goal)
            db.add(GoalApproval(goal_id=goal.id, stage="manager", action="approve", comment=comment.strip(), actor_emp_no=user.emp_no))
            db.commit()
            st.success("承認しました（HRへ提出）。")

            emp = db.execute(select(Employee).where(Employee.emp_no == goal.employee_emp_no)).scalar_one_or_none()
            hr_admins = db.execute(select(Employee).where(Employee.role_admin == True)).scalars().all()
            for hr_admin in hr_admins:
                if hr_admin.email:
                    send_email(
                        hr_admin.email,
                        f"【目標管理】{emp.name if emp else ''}さんの目標が上長承認されました",
                        f"こんにちは,\n\n{emp.name if emp else ''}さんの{goal.year}年度の目標が上長（{user.name}さん）に承認されました。\n確認をお願いいたします。\n\n---\n本メールは自動送信されています。",
                    )
            st.rerun()

        if ret:
            if not comment.strip():
                st.error("差し戻し時はコメントが必須です。")
                st.stop()
            goal = attach(db, goal)
            goal.status = "manager_returned"
            db.add(goal)
            db.add(GoalApproval(goal_id=goal.id, stage="manager", action="return", comment=comment.strip(), actor_emp_no=user.emp_no))
            db.commit()
            st.warning("差し戻しました。")
            st.rerun()


def page_goal_approve_hr() -> None:
    user = require_role("HR管理者")
    year = get_selected_year()
    st.subheader("Confirm Goals (HR)")

    with SessionLocal() as db:
        candidates = db.execute(
            select(Goal).where(Goal.year == year, Goal.status == "manager_approved").order_by(Goal.updated_at.desc())
        ).scalars().all()
        if not candidates:
            st.info("HR確認待ちの目標がありません。")
            return

        options: Dict[str, int] = {}
        for g in candidates:
            emp = db.execute(select(Employee).where(Employee.emp_no == g.employee_emp_no)).scalar_one_or_none()
            options[f"{g.employee_emp_no} {emp.name if emp else ''}（更新 {g.updated_at:%Y-%m-%d}）"] = g.id

        label = st.selectbox("対象を選択", list(options.keys()))
        goal_id = options[label]
        goal = db.execute(select(Goal).where(Goal.id == goal_id)).scalar_one()
        items = db.execute(select(GoalItem).where(GoalItem.goal_id == goal.id).order_by(GoalItem.id.asc())).scalars().all()

        st.caption(f"年度: {year} / 状態: {status_label_goal(goal.status)}")
        st.markdown("### 目標内容")
        for it in items:
            if it.type == "business":
                with st.expander(f"Business（Weight {it.weight}）"):
                    st.write(f"**① 対象業務**: {it.specific}")
                    st.write(f"**② 達成したい結果**: {it.measurable}")
                    st.write(f"**③ 期限**: {it.time_bound}")
                    st.write(f"**④ 部署ゴールとの関連性**: {it.relevant}")
                    st.write(f"**実行計画（任意）**: {it.achievable}")
            else:
                with st.expander("Development"):
                    st.write(f"**① なりたい人物像/身につけたいスキル**: {it.specific}")
                    st.write(f"**② 現在の自分とのギャップ**: {it.measurable}")
                    st.write(f"**③ 行動**: {it.relevant}")
                    st.write(f"**実行計画（任意）**: {it.achievable}")
                    st.write(f"**完了時期（任意）**: {it.time_bound or '（未設定）'}")

        comment = st.text_area("コメント（差し戻し時は必須）", key=f"gh_comment_{goal_id}")
        c1, c2 = st.columns(2)
        with c1:
            approve = st.button("HR確認（公開）", use_container_width=True)
        with c2:
            ret = st.button("差し戻し（社員へ）", use_container_width=True)

        if approve:
            goal = attach(db, goal)
            goal.status = "hr_approved"
            db.add(goal)
            db.add(GoalApproval(goal_id=goal.id, stage="hr", action="approve", comment=comment.strip(), actor_emp_no=user.emp_no))
            db.commit()
            st.success("HR確認しました（公開）。")

            emp = db.execute(select(Employee).where(Employee.emp_no == goal.employee_emp_no)).scalar_one_or_none()
            manager = db.execute(select(Employee).where(Employee.emp_no == emp.manager_emp_no)).scalar_one_or_none() if emp else None
            if manager and manager.email:
                send_email(
                    manager.email,
                    f"【目標管理】{emp.name if emp else ''}さんの目標がHR確認されました",
                    f"こんにちは {manager.name}さん,\n\n{emp.name if emp else ''}さんの{goal.year}年度の目標がHRにより最終確認されました。\n公開されています。\n\n---\n本メールは自動送信されています。",
                )
            st.rerun()

        if ret:
            if not comment.strip():
                st.error("差し戻し時はコメントが必須です。")
                st.stop()
            goal = attach(db, goal)
            goal.status = "hr_returned"
            db.add(goal)
            db.add(GoalApproval(goal_id=goal.id, stage="hr", action="return", comment=comment.strip(), actor_emp_no=user.emp_no))
            db.commit()
            st.warning("差し戻しました（社員が修正→再Submit）。")
            st.rerun()


# ---- Part 3 continues (Evaluation / 1on1 / Dashboard / Export / Router / main) ----


# ---- Part 3 ----
# Evaluation / 1on1 / Approval status / HR dashboard / CSV export / Router / main
# (All pages use session-fixed year via get_selected_year())

def page_eval_input_self() -> None:
    user = require_role("入力者")
    year = get_selected_year()
    st.subheader("Self Evaluation")
    st.caption(f"年度: {year}")

    with SessionLocal() as db:
        try:
            goal = ensure_goal_exists_for_eval(db, user.emp_no, year)
        except ValueError as e:
            st.error(str(e))
            return

        ev = get_or_create_evaluation(db, user.emp_no, year, goal.id)
        st.caption(f"評価状態: {status_label_eval(ev.status)}")
        if not can_edit_eval_self(ev.status):
            st.info("この評価は編集できない状態です。")
            return

        items = db.execute(select(GoalItem).where(GoalItem.goal_id == goal.id).order_by(GoalItem.id.asc())).scalars().all()
        biz = [it for it in items if it.type == "business"]

        st.markdown("### what（business goal 達成度）")
        st.caption("各business goalの達成率を入力してください（0〜200%）。")
        state_key = f"es:biz:{user.emp_no}:{year}"
        if state_key not in st.session_state:
            st.session_state[state_key] = [{"id": it.id, "達成率%(0-200)": it.achieved_percent or 0} for it in biz]

        editor_rows = []
        for it in biz:
            editor_rows.append(
                {
                    "目標ID": it.id,
                    "Weight": it.weight or 0,
                    "対象業務": (it.specific or "")[:50] + ("…" if it.specific and len(it.specific) > 50 else ""),
                    "達成率%(0-200)": next((r["達成率%(0-200)"] for r in st.session_state[state_key] if r["id"] == it.id), it.achieved_percent or 0),
                }
            )
        updated = st.data_editor(editor_rows, num_rows="fixed", use_container_width=True)

        tmp = [{"id": int(r["目標ID"]), "達成率%(0-200)": int(r["達成率%(0-200)"])} for r in updated]
        st.session_state[state_key] = tmp

        for it in biz:
            newp = next((r["達成率%(0-200)"] for r in tmp if r["id"] == it.id), it.achieved_percent or 0)
            it.achieved_percent = int(newp)

        pct, what = calc_what_from_business(biz)
        st.info(f"what（自己）プレビュー: {what}（加重平均 {pct:.1f}%）")

        st.markdown("---")
        st.markdown("### how（8カテゴリ×5問 / 1〜4点）")
        questions = db.execute(select(HowQuestion).where(HowQuestion.active.is_(True))).scalars().all()
        qmap: Dict[str, List[HowQuestion]] = {}
        for q in questions:
            qmap.setdefault(q.category_key, []).append(q)
        for k in qmap:
            qmap[k].sort(key=lambda x: x.question_no)

        existing = db.execute(select(HowAnswer).where(HowAnswer.evaluation_id == ev.id, HowAnswer.rater == "self")).scalars().all()
        existing_map = {(a.category_key, a.question_no): a.score for a in existing}

        scores: List[int] = []
        for cat_key, cat_label in HOW_CATEGORIES:
            st.markdown(f"#### {cat_label}")
            for q in qmap.get(cat_key, []):
                key = f"es_ans:{ev.id}:{cat_key}:{q.question_no}"
                default = int(existing_map.get((cat_key, q.question_no), 3))
                val = st.radio(
                    q.question_text,
                    options=[1, 2, 3, 4],
                    index=[1, 2, 3, 4].index(default),
                    horizontal=True,
                    key=key,
                )
                scores.append(int(val))

        total, ratio, how = calc_how_from_scores(scores)
        st.info(f"how（自己）プレビュー: {how}（得点 {total}/160, 得点率 {ratio*100:.1f}%）")

        st.markdown("---")
        comment = st.text_area("自己コメント", value=ev.self_comment or "", key=f"es_comment:{ev.id}")

        c1, c2 = st.columns(2)
        with c1:
            save = st.button("保存（下書き）", use_container_width=True)
        with c2:
            submit = st.button("自己評価を提出（上長へ）", use_container_width=True)

        if save or submit:
            for r in tmp:
                if r["達成率%(0-200)"] < 0 or r["達成率%(0-200)"] > 200:
                    st.error("達成率%は0〜200です。")
                    st.stop()

            for it in biz:
                newp = next((r["達成率%(0-200)"] for r in tmp if r["id"] == it.id), it.achieved_percent or 0)
                it.achieved_percent = int(newp)
                db.add(it)
            db.flush()

            for a in db.execute(select(HowAnswer).where(HowAnswer.evaluation_id == ev.id, HowAnswer.rater == "self")).scalars().all():
                db.delete(a)
            db.flush()

            for cat_key, _ in HOW_CATEGORIES:
                for q in qmap.get(cat_key, []):
                    sc = int(st.session_state[f"es_ans:{ev.id}:{cat_key}:{q.question_no}"])
                    db.add(HowAnswer(evaluation_id=ev.id, rater="self", category_key=cat_key, question_no=q.question_no, score=sc))

            _, what2 = calc_what_from_business(biz)
            _, _, how2 = calc_how_from_scores(scores)

            ev.what_self = what2
            ev.how_self = how2
            ev.self_comment = comment.strip()
            ev.status = "draft" if save else "submitted_self"
            db.add(ev)
            db.add(
                EvaluationApproval(
                    evaluation_id=ev.id,
                    stage="self",
                    action="save" if save else "submit",
                    comment="",
                    actor_emp_no=user.emp_no,
                )
            )
            db.commit()
            st.success("保存しました。" if save else "提出しました（上長評価待ち）。")

            if submit:
                emp = db.execute(select(Employee).where(Employee.emp_no == user.emp_no)).scalar_one_or_none()
                emp_email = emp.email if emp else None
                manager = db.execute(select(Employee).where(Employee.emp_no == emp.manager_emp_no)).scalar_one_or_none() if emp else None
                manager_email = manager.email if manager else None

                if emp_email:
                    send_email(
                        emp_email,
                        f"【評価】自己評価を提出しました（{year}年度）",
                        f"こんにちは {user.name}さん,\n\nあなたが自己評価を提出しました。\n上長による評価をお待ちしています。\n\n---\n本メールは自動送信されています。",
                    )
                if manager_email:
                    send_email(
                        manager_email,
                        f"【評価】{user.name}さんが自己評価を提出しました（{year}年度）",
                        f"こんにちは,\n\n{user.name}さんが{year}年度の自己評価を提出しました。\n評価をお願いいたします。\n\n---\n本メールは自動送信されています。",
                    )

            st.rerun()


def page_eval_input_manager() -> None:
    user = require_role("評価者")
    year = get_selected_year()
    st.subheader("Evaluate Team Members")
    st.caption(f"年度: {year}")

    with SessionLocal() as db:
        subs = db.execute(select(Employee).where(Employee.manager_emp_no == user.emp_no, Employee.active.is_(True))).scalars().all()
        if not subs:
            st.info("部下がいません。")
            return

        sub_options = {f"{e.emp_no} {e.name}": e.emp_no for e in subs}
        target_label = st.selectbox("部下を選択", list(sub_options.keys()))
        emp_no = sub_options[target_label]

        ev = db.execute(select(Evaluation).where(Evaluation.employee_emp_no == emp_no, Evaluation.year == year)).scalar_one_or_none()
        if not ev:
            st.info("評価がまだ開始されていません（社員が自己評価を作成してください）。")
            return

        if ev.status not in {"submitted_self", "hr_returned"}:
            if ev.status == "manager_returned":
                st.info("差し戻し中です（社員の再提出待ち）。")
            else:
                st.info(f"この評価は上長入力フェーズではありません（状態: {status_label_eval(ev.status)}）。")
            return

        goal = db.execute(select(Goal).where(Goal.id == ev.goal_id)).scalar_one()
        items = db.execute(select(GoalItem).where(GoalItem.goal_id == goal.id).order_by(GoalItem.id.asc())).scalars().all()
        biz = [it for it in items if it.type == "business"]

        st.caption(f"評価状態: {status_label_eval(ev.status)}")
        st.markdown("### what（達成度合い）")
        pct, what_auto = calc_what_from_business(biz)
        st.info(f"加重平均（達成率）: {pct:.1f}% / 自動判定: {what_auto}")

        what_manager = st.selectbox(
            "what（上長）最終選択",
            options=["exceeds", "meets", "does_not_meet"],
            index=["exceeds", "meets", "does_not_meet"].index(ev.what_manager or what_auto),
        )

        st.markdown("---")
        st.markdown("### how（上長評価）")
        questions = db.execute(select(HowQuestion).where(HowQuestion.active.is_(True))).scalars().all()
        qmap: Dict[str, List[HowQuestion]] = {}
        for q in questions:
            qmap.setdefault(q.category_key, []).append(q)
        for k in qmap:
            qmap[k].sort(key=lambda x: x.question_no)

        existing = db.execute(select(HowAnswer).where(HowAnswer.evaluation_id == ev.id, HowAnswer.rater == "manager")).scalars().all()
        existing_map = {(a.category_key, a.question_no): a.score for a in existing}

        scores: List[int] = []
        for cat_key, cat_label in HOW_CATEGORIES:
            st.markdown(f"#### {cat_label}")
            for q in qmap.get(cat_key, []):
                key = f"em_ans:{ev.id}:{cat_key}:{q.question_no}"
                default = int(existing_map.get((cat_key, q.question_no), 3))
                val = st.radio(
                    q.question_text,
                    options=[1, 2, 3, 4],
                    index=[1, 2, 3, 4].index(default),
                    horizontal=True,
                    key=key,
                )
                scores.append(int(val))

        total, ratio, how_mgr = calc_how_from_scores(scores)
        st.info(f"how（上長）プレビュー: {how_mgr}（得点 {total}/160, 得点率 {ratio*100:.1f}%）")

        st.markdown("---")
        comment = st.text_area("上長コメント（差し戻し時は必須）", value=ev.manager_comment or "", key=f"em_comment:{ev.id}")
        c1, c2, c3 = st.columns(3)
        with c1:
            save = st.button("保存（下書き）", use_container_width=True)
        with c2:
            submit = st.button("提出（HRへ）", use_container_width=True)
        with c3:
            ret = st.button("差し戻し（社員へ）", use_container_width=True)

        if save or submit or ret:
            if ret and not comment.strip():
                st.error("差し戻し時はコメントが必須です。")
                st.stop()

            for a in db.execute(select(HowAnswer).where(HowAnswer.evaluation_id == ev.id, HowAnswer.rater == "manager")).scalars().all():
                db.delete(a)
            db.flush()

            for cat_key, _ in HOW_CATEGORIES:
                for q in qmap.get(cat_key, []):
                    sc = int(st.session_state[f"em_ans:{ev.id}:{cat_key}:{q.question_no}"])
                    db.add(HowAnswer(evaluation_id=ev.id, rater="manager", category_key=cat_key, question_no=q.question_no, score=sc))

            _, _, how_mgr2 = calc_how_from_scores(scores)
            ev.what_manager = what_manager
            ev.how_manager = how_mgr2
            ev.manager_comment = comment.strip()

            if save:
                ev.status = "submitted_self"
                act = "save"
            elif submit:
                ev.status = "manager_submitted"
                act = "submit"
            else:
                ev.status = "manager_returned"
                act = "return"

            db.add(ev)
            db.add(
                EvaluationApproval(
                    evaluation_id=ev.id,
                    stage="manager",
                    action=act,
                    comment=comment.strip() if act == "return" else "",
                    actor_emp_no=user.emp_no,
                )
            )
            db.commit()
            st.success("保存しました。" if save else ("提出しました（HR確認待ち）。" if submit else "差し戻しました。"))

            if submit:
                emp = db.execute(select(Employee).where(Employee.emp_no == ev.employee_emp_no)).scalar_one_or_none()
                hr_admins = db.execute(select(Employee).where(Employee.role_admin == True)).scalars().all()
                for hr_admin in hr_admins:
                    if hr_admin.email:
                        send_email(
                            hr_admin.email,
                            f"【評価】{emp.name if emp else ''}さんの{ev.year}年度評価が上長提出されました",
                            f"こんにちは,\n\n{emp.name if emp else ''}さんの{ev.year}年度評価が上長（{user.name}さん）により提出されました。\nHR確認をお願いいたします。\n\n---\n本メールは自動送信されています。",
                        )
            st.rerun()


def page_eval_approve_hr() -> None:
    user = require_role("HR管理者")
    year = get_selected_year()
    st.subheader("Confirm Evaluations (HR)")
    st.caption(f"年度: {year}")

    with SessionLocal() as db:
        candidates = db.execute(
            select(Evaluation).where(Evaluation.year == year, Evaluation.status == "manager_submitted").order_by(Evaluation.updated_at.desc())
        ).scalars().all()
        if not candidates:
            st.info("HR確認待ちの評価がありません。")
            return

        options: Dict[str, int] = {}
        for ev in candidates:
            emp = db.execute(select(Employee).where(Employee.emp_no == ev.employee_emp_no)).scalar_one_or_none()
            options[f"{ev.employee_emp_no} {emp.name if emp else ''}（更新 {ev.updated_at:%Y-%m-%d}）"] = ev.id

        label = st.selectbox("対象を選択", list(options.keys()))
        ev_id = options[label]
        ev = db.execute(select(Evaluation).where(Evaluation.id == ev_id)).scalar_one()

        goal = db.execute(select(Goal).where(Goal.id == ev.goal_id)).scalar_one()
        items = db.execute(select(GoalItem).where(GoalItem.goal_id == goal.id)).scalars().all()
        biz = [it for it in items if it.type == "business"]

        pct, what_auto = calc_what_from_business(biz)
        st.write(f"what: self={ev.what_self} / manager={ev.what_manager} / auto={what_auto}（{pct:.1f}%）")
        st.write(f"how: self={ev.how_self} / manager={ev.how_manager}")
        st.write(f"自己コメント: {ev.self_comment}")
        st.write(f"上長コメント: {ev.manager_comment}")

        st.markdown("### how レーダー（自己 vs 上長）")
        self_ans = db.execute(select(HowAnswer).where(HowAnswer.evaluation_id == ev.id, HowAnswer.rater == "self")).scalars().all()
        mgr_ans = db.execute(select(HowAnswer).where(HowAnswer.evaluation_id == ev.id, HowAnswer.rater == "manager")).scalars().all()
        radar_chart(category_averages(self_ans), category_averages(mgr_ans))

        st.markdown("---")
        comment = st.text_area("HRコメント（差し戻し時は必須）", value=ev.hr_comment or "", key=f"eh_comment:{ev.id}")
        c1, c2 = st.columns(2)
        with c1:
            approve = st.button("HR確認（確定/公開）", use_container_width=True)
        with c2:
            ret = st.button("差し戻し（上長へ）", use_container_width=True)

        if approve:
            ev.status = "hr_approved"
            ev.what_final = ev.what_manager or what_auto
            ev.how_final = ev.how_manager or (ev.how_self or "does_not_meet")
            ev.hr_comment = comment.strip()
            db.add(ev)
            db.add(EvaluationApproval(evaluation_id=ev.id, stage="hr", action="approve", comment=comment.strip(), actor_emp_no=user.emp_no))
            db.commit()
            st.success("HR確認しました。上長は1on1設定ができます。")

            emp = db.execute(select(Employee).where(Employee.emp_no == ev.employee_emp_no)).scalar_one_or_none()
            manager = db.execute(select(Employee).where(Employee.emp_no == emp.manager_emp_no)).scalar_one_or_none() if emp else None

            if emp and emp.email:
                send_email(
                    emp.email,
                    f"【評価】{ev.year}年度評価がHR確認されました",
                    f"こんにちは {emp.name}さん,\n\nあなたの{ev.year}年度評価がHRにより確認されました。\n最終確定されています。\n\n---\n本メールは自動送信されています。",
                )
            if manager and manager.email:
                send_email(
                    manager.email,
                    f"【評価】{emp.name if emp else ''}さんの{ev.year}年度評価がHR確認されました",
                    f"こんにちは {manager.name}さん,\n\n{emp.name if emp else ''}さんの{ev.year}年度評価がHRにより確認されました。\n最終確定されています。1on1設定をお知らせください。\n\n---\n本メールは自動送信されています。",
                )

            st.rerun()

        if ret:
            if not comment.strip():
                st.error("差し戻し時はコメントが必須です。")
                st.stop()
            ev.status = "hr_returned"
            ev.hr_comment = comment.strip()
            db.add(ev)
            db.add(EvaluationApproval(evaluation_id=ev.id, stage="hr", action="return", comment=comment.strip(), actor_emp_no=user.emp_no))
            db.commit()
            st.warning("差し戻しました（上長が修正→再提出）。")
            st.rerun()


def page_eval_view_self() -> None:
    user = require_role("入力者")
    year = get_selected_year()
    st.subheader("My Evaluations")
    st.caption(f"年度: {year}")

    with SessionLocal() as db:
        ev = db.execute(select(Evaluation).where(Evaluation.employee_emp_no == user.emp_no, Evaluation.year == year)).scalar_one_or_none()
        if not ev:
            st.info("評価がありません。")
            return

        st.caption(f"状態: {status_label_eval(ev.status)}")
        st.write(f"what: self={ev.what_self} / manager={ev.what_manager} / final={ev.what_final}")
        st.write(f"how: self={ev.how_self} / manager={ev.how_manager} / final={ev.how_final}")
        st.write(f"自己コメント: {ev.self_comment}")
        st.write(f"上長コメント: {ev.manager_comment}")
        st.write(f"HRコメント: {ev.hr_comment}")

        st.markdown("### how レーダー（自己 vs 上長）")
        self_ans = db.execute(select(HowAnswer).where(HowAnswer.evaluation_id == ev.id, HowAnswer.rater == "self")).scalars().all()
        mgr_ans = db.execute(select(HowAnswer).where(HowAnswer.evaluation_id == ev.id, HowAnswer.rater == "manager")).scalars().all()
        radar_chart(category_averages(self_ans), category_averages(mgr_ans))

        st.markdown("---")
        st.markdown("### 承認ログ")
        approvals = db.execute(select(EvaluationApproval).where(EvaluationApproval.evaluation_id == ev.id).order_by(EvaluationApproval.acted_at.asc())).scalars().all()
        rows = [
            {
                "日時": a.acted_at.strftime("%Y-%m-%d %H:%M"),
                "ステージ": a.stage,
                "アクション": a.action,
                "実行者": a.actor_emp_no,
                "コメント": (a.comment or "").strip(),
            }
            for a in approvals
        ]
        st.dataframe(rows, use_container_width=True)


def page_oneonone_manager() -> None:
    user = require_role("評価者")
    year = get_selected_year()
    st.subheader("Schedule 1:1 Meeting")
    st.caption(f"年度: {year}")

    with SessionLocal() as db:
        subs = db.execute(select(Employee).where(Employee.manager_emp_no == user.emp_no, Employee.active.is_(True))).scalars().all()
        if not subs:
            st.info("部下がいません。")
            return

        sub_options = {f"{e.emp_no} {e.name}": e.emp_no for e in subs}
        target_label = st.selectbox("部下を選択", list(sub_options.keys()))
        emp_no = sub_options[target_label]

        ev = db.execute(select(Evaluation).where(Evaluation.employee_emp_no == emp_no, Evaluation.year == year)).scalar_one_or_none()
        if not ev or ev.status != "hr_approved":
            st.info("HR確認済み評価がありません（HR確認後に1on1設定できます）。")
            return

        o1 = db.execute(select(OneOnOne).where(OneOnOne.evaluation_id == ev.id)).scalar_one_or_none()
        if not o1:
            o1 = OneOnOne(evaluation_id=ev.id, manager_emp_no=user.emp_no, employee_emp_no=emp_no, status="draft")
            db.add(o1)
            db.commit()
            db.refresh(o1)

        st.caption(f"1on1状態: {o1.status}")

        slot1_d = st.date_input("候補1（日付）", value=datetime.utcnow().date(), key=f"o1_slot1_d:{o1.id}")
        slot1_t = st.time_input("候補1（時刻）", value=datetime.utcnow().time().replace(second=0, microsecond=0), key=f"o1_slot1_t:{o1.id}")

        slot2_d = st.date_input("候補2（日付）", value=datetime.utcnow().date(), key=f"o1_slot2_d:{o1.id}")
        slot2_t = st.time_input("候補2（時刻）", value=datetime.utcnow().time().replace(second=0, microsecond=0), key=f"o1_slot2_t:{o1.id}")

        slot3_d = st.date_input("候補3（日付）", value=datetime.utcnow().date(), key=f"o1_slot3_d:{o1.id}")
        slot3_t = st.time_input("候補3（時刻）", value=datetime.utcnow().time().replace(second=0, microsecond=0), key=f"o1_slot3_t:{o1.id}")

        location = st.text_input("場所/オンラインURL", value=o1.location or "", key=f"o1_loc:{o1.id}")
        note = st.text_area("メモ", value=o1.note or "", key=f"o1_note:{o1.id}")

        c1, c2 = st.columns(2)
        with c1:
            propose = st.button("提案（社員へ）", use_container_width=True)
        with c2:
            save = st.button("保存（下書き）", use_container_width=True)

        if save or propose:
            o1.slot1 = datetime.combine(slot1_d, slot1_t)
            o1.slot2 = datetime.combine(slot2_d, slot2_t)
            o1.slot3 = datetime.combine(slot3_d, slot3_t)
            o1.location = location.strip()
            o1.note = note.strip()
            if propose:
                o1.status = "proposed"
            db.add(o1)
            db.commit()
            st.success("保存しました。" if save else "提案しました（社員が承認します）。")

            if propose:
                emp = db.execute(select(Employee).where(Employee.emp_no == o1.evaluation.employee_emp_no)).scalar_one_or_none()
                manager = db.execute(select(Employee).where(Employee.emp_no == user.emp_no)).scalar_one_or_none()

                if emp and emp.email:
                    send_email(
                        emp.email,
                        f"【1on1】{user.name}さんが面談日時を提案しました",
                        "こんにちは {name}さん,\n\n{mgr}さんが1on1面談の日時候補を提案しました。\n確認して選択してください。\n\n候補日時：\n- 候補1: {s1}\n- 候補2: {s2}\n- 候補3: {s3}\n\n---\n本メールは自動送信されています。".format(
                            name=emp.name,
                            mgr=user.name,
                            s1=o1.slot1.strftime("%Y-%m-%d %H:%M") if o1.slot1 else "N/A",
                            s2=o1.slot2.strftime("%Y-%m-%d %H:%M") if o1.slot2 else "N/A",
                            s3=o1.slot3.strftime("%Y-%m-%d %H:%M") if o1.slot3 else "N/A",
                        ),
                    )

                if manager and manager.email:
                    send_email(
                        manager.email,
                        f"【1on1】面談日時候補を{emp.name if emp else ''}さんに提案しました",
                        f"こんにちは {user.name}さん,\n\n1on1面談の日時候補を{emp.name if emp else ''}さんに提案しました。\n承認をお待ちしています。\n\n---\n本メールは自動送信されています。",
                    )
            st.rerun()

        st.markdown("---")
        if o1.status == "confirmed":
            st.success(f"確定: 候補{o1.selected_slot} が選択されました。")
        elif o1.status == "proposed":
            st.info("社員の承認待ちです。")
        else:
            st.info("下書き状態です。")


def page_oneonone_employee() -> None:
    user = require_role("入力者")
    year = get_selected_year()
    st.subheader("1:1 Review (Employee)")
    st.caption(f"年度: {year}")

    with SessionLocal() as db:
        ev = db.execute(select(Evaluation).where(Evaluation.employee_emp_no == user.emp_no, Evaluation.year == year)).scalar_one_or_none()
        if not ev:
            st.info("評価がありません。")
            return

        o1 = db.execute(select(OneOnOne).where(OneOnOne.evaluation_id == ev.id)).scalar_one_or_none()
        if not o1 or o1.status not in {"proposed", "confirmed"}:
            st.info("1on1提案がありません。")
            return

        st.caption(f"状態: {o1.status}")
        st.write(f"場所/URL: {o1.location}")
        st.write(f"メモ: {o1.note}")

        slots = []
        if o1.slot1:
            slots.append((1, o1.slot1))
        if o1.slot2:
            slots.append((2, o1.slot2))
        if o1.slot3:
            slots.append((3, o1.slot3))

        if o1.status == "confirmed":
            chosen = next((d for n, d in slots if n == o1.selected_slot), None)
            st.success(f"確定日時: {chosen.strftime('%Y-%m-%d %H:%M') if chosen else ''}")
            return

        st.markdown("### 候補から選択して承認してください")
        options = {f"候補{n}: {d.strftime('%Y-%m-%d %H:%M')}": n for n, d in slots}
        pick_label = st.selectbox("選択", list(options.keys()))
        pick = options[pick_label]
        confirm = st.button("この日時で承認（確定）", use_container_width=True)

        if confirm:
            o1.selected_slot = int(pick)
            o1.status = "confirmed"
            db.add(o1)
            db.commit()
            st.success("承認しました（確定）。")

            emp = db.execute(select(Employee).where(Employee.emp_no == user.emp_no)).scalar_one_or_none()
            manager = db.execute(select(Employee).where(Employee.emp_no == emp.manager_emp_no)).scalar_one_or_none() if emp else None
            chosen_date = next((d for n, d in slots if n == pick), None)

            if emp and emp.email:
                send_email(
                    emp.email,
                    "【1on1】面談日時が確定しました",
                    f"こんにちは {user.name}さん,\n\n1on1面談の日時が確定しました。\n\n日時: {chosen_date.strftime('%Y-%m-%d %H:%M') if chosen_date else 'N/A'}\n場所/URL: {o1.location}\n\n---\n本メールは自動送信されています。",
                )
            if manager and manager.email:
                send_email(
                    manager.email,
                    f"【1on1】{emp.name if emp else ''}さんとの面談日時が確定しました",
                    f"こんにちは {manager.name}さん,\n\n{emp.name if emp else ''}さんとの1on1面談の日時が確定しました。\n\n日時: {chosen_date.strftime('%Y-%m-%d %H:%M') if chosen_date else 'N/A'}\n場所/URL: {o1.location}\n\n---\n本メールは自動送信されています。",
                )
            st.rerun()


def page_approval_status_self() -> None:
    user = require_role("入力者")
    year = get_selected_year()
    st.subheader("Approval Status")
    st.caption(f"年度: {year}")

    with SessionLocal() as db:
        goal = db.execute(select(Goal).where(Goal.employee_emp_no == user.emp_no, Goal.year == year)).scalar_one_or_none()
        ev = db.execute(select(Evaluation).where(Evaluation.employee_emp_no == user.emp_no, Evaluation.year == year)).scalar_one_or_none()

        st.markdown("### 目標")
        if not goal:
            st.write("未作成")
        else:
            st.write(f"状態: {status_label_goal(goal.status)}")
            approvals = db.execute(select(GoalApproval).where(GoalApproval.goal_id == goal.id).order_by(GoalApproval.acted_at.asc())).scalars().all()
            rows = [{"日時": a.acted_at.strftime("%Y-%m-%d %H:%M"), "ステージ": a.stage, "アクション": a.action, "コメント": (a.comment or "").strip()} for a in approvals]
            st.dataframe(rows, use_container_width=True)

        st.markdown("### 評価")
        if not ev:
            st.write("未開始")
        else:
            st.write(f"状態: {status_label_eval(ev.status)}")
            approvals = db.execute(select(EvaluationApproval).where(EvaluationApproval.evaluation_id == ev.id).order_by(EvaluationApproval.acted_at.asc())).scalars().all()
            rows = [{"日時": a.acted_at.strftime("%Y-%m-%d %H:%M"), "ステージ": a.stage, "アクション": a.action, "コメント": (a.comment or "").strip()} for a in approvals]
            st.dataframe(rows, use_container_width=True)


def page_hr_dashboard() -> None:
    require_role("HR管理者")
    year = get_selected_year()
    st.subheader("HR Dashboard")
    st.caption(f"年度: {year}")

    with SessionLocal() as db:
        employees = db.execute(select(Employee).where(Employee.active.is_(True))).scalars().all()
        managers = [e for e in employees if e.role_manager]
        departments = sorted({e.department for e in employees if e.department})

        dept = st.selectbox("部署で絞り込み", options=["(全て)"] + departments)
        mgr_opts: Dict[str, Optional[str]] = {"(全て)": None}
        for m in sorted(managers, key=lambda x: x.emp_no):
            mgr_opts[f"{m.emp_no} {m.name}"] = m.emp_no
        mgr_label = st.selectbox("マネージャーで絞り込み", options=list(mgr_opts.keys()))
        mgr_emp_no = mgr_opts[mgr_label]

        def emp_filter(e: Employee) -> bool:
            if dept != "(全て)" and e.department != dept:
                return False
            if mgr_emp_no and e.manager_emp_no != mgr_emp_no:
                return False
            return True

        filtered_emps = [e for e in employees if emp_filter(e) and e.role_employee]
        filtered_emp_nos = {e.emp_no for e in filtered_emps}

        goals = db.execute(select(Goal).where(Goal.year == year)).scalars().all()
        evals = db.execute(select(Evaluation).where(Evaluation.year == year)).scalars().all()

        goals_f = [g for g in goals if g.employee_emp_no in filtered_emp_nos]
        evals_f = [e for e in evals if e.employee_emp_no in filtered_emp_nos]

        st.metric("入力者（対象）", value=str(len(filtered_emps)))

        goal_counts = {k: 0 for k in GOAL_STATUSES.keys()}
        for g in goals_f:
            goal_counts[g.status] = goal_counts.get(g.status, 0) + 1

        eval_counts = {k: 0 for k in EVAL_STATUSES.keys()}
        for e in evals_f:
            eval_counts[e.status] = eval_counts.get(e.status, 0) + 1

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("### 目標 進捗（件数）")
            st.dataframe([{"status": GOAL_STATUSES[k], "count": goal_counts.get(k, 0)} for k in GOAL_STATUSES.keys()], use_container_width=True)
        with c2:
            st.markdown("### 評価 進捗（件数）")
            st.dataframe([{"status": EVAL_STATUSES[k], "count": eval_counts.get(k, 0)} for k in EVAL_STATUSES.keys()], use_container_width=True)

        def ratio(n: int, d: int) -> float:
            return (n / d * 100.0) if d else 0.0

        total_emp = len(filtered_emps)
        goal_created = len({g.employee_emp_no for g in goals_f})
        goal_submitted_or_more = len({g.employee_emp_no for g in goals_f if g.status in {"submitted", "manager_approved", "manager_returned", "hr_returned", "hr_approved"}})
        goal_hr_approved = len({g.employee_emp_no for g in goals_f if g.status == "hr_approved"})

        eval_started = len({e.employee_emp_no for e in evals_f})
        eval_manager_submitted = len({e.employee_emp_no for e in evals_f if e.status in {"manager_submitted", "hr_returned", "hr_approved"}})
        eval_hr_approved = len({e.employee_emp_no for e in evals_f if e.status == "hr_approved"})

        st.markdown("### 進捗割合（対象人数に対する比率）")
        r1, r2, r3 = st.columns(3)
        r1.metric("目標作成率", f"{ratio(goal_created, total_emp):.1f}%")
        r2.metric("目標提出率", f"{ratio(goal_submitted_or_more, total_emp):.1f}%")
        r3.metric("目標HR確認率", f"{ratio(goal_hr_approved, total_emp):.1f}%")

        r4, r5, r6 = st.columns(3)
        r4.metric("評価開始率", f"{ratio(eval_started, total_emp):.1f}%")
        r5.metric("評価上長提出率", f"{ratio(eval_manager_submitted, total_emp):.1f}%")
        r6.metric("評価HR確認率", f"{ratio(eval_hr_approved, total_emp):.1f}%")

        st.markdown("---")
        st.markdown("### 一覧（フィルタ結果）")
        goal_map = {g.employee_emp_no: g for g in goals_f}
        eval_map = {e.employee_emp_no: e for e in evals_f}
        rows = []
        for emp in sorted(filtered_emps, key=lambda x: x.emp_no):
            g = goal_map.get(emp.emp_no)
            e = eval_map.get(emp.emp_no)
            rows.append(
                {
                    "emp_no": emp.emp_no,
                    "name": emp.name,
                    "department": emp.department,
                    "manager_emp_no": emp.manager_emp_no or "",
                    "goal_status": status_label_goal(g.status) if g else "未作成",
                    "eval_status": status_label_eval(e.status) if e else "未開始",
                }
            )
        st.dataframe(rows, use_container_width=True)


def page_admin_csv() -> None:
    require_role("HR管理者")
    year = get_selected_year()
    st.subheader("Export CSV")
    st.caption(f"年度: {year}")

    with SessionLocal() as db:
        st.markdown("### 目標CSV（上長提出時点でDL可）")
        goals = db.execute(select(Goal).where(Goal.year == year)).scalars().all()
        export_goals = [g for g in goals if g.status in {"manager_approved", "hr_returned", "hr_approved"}]
        rows: List[Dict[str, Any]] = []
        for g in export_goals:
            emp = db.execute(select(Employee).where(Employee.emp_no == g.employee_emp_no)).scalar_one_or_none()
            items = db.execute(select(GoalItem).where(GoalItem.goal_id == g.id)).scalars().all()
            for it in items:
                rows.append(
                    {
                        "year": g.year,
                        "employee_emp_no": g.employee_emp_no,
                        "employee_name": emp.name if emp else "",
                        "department": emp.department if emp else "",
                        "manager_emp_no": emp.manager_emp_no if emp else "",
                        "goal_status": g.status,
                        "goal_type": it.type,
                        "specific": it.specific,
                        "measurable": it.measurable,
                        "achievable": it.achievable,
                        "relevant": it.relevant,
                        "time_bound": it.time_bound,
                        "weight": it.weight if it.type == "business" else "",
                        "achieved_percent": it.achieved_percent if it.type == "business" else "",
                        "updated_at": g.updated_at.isoformat(),
                    }
                )
        st.download_button("goals.csv をダウンロード", data=to_csv(rows).encode("utf-8"), file_name="goals.csv", mime="text/csv")

        st.markdown("---")
        st.markdown("### 評価CSV（上長提出時点でDL可）")
        evs = db.execute(select(Evaluation).where(Evaluation.year == year)).scalars().all()
        export_evs = [e for e in evs if e.status in {"manager_submitted", "hr_returned", "hr_approved"}]
        rows2: List[Dict[str, Any]] = []
        for e in export_evs:
            emp = db.execute(select(Employee).where(Employee.emp_no == e.employee_emp_no)).scalar_one_or_none()
            rows2.append(
                {
                    "year": e.year,
                    "employee_emp_no": e.employee_emp_no,
                    "employee_name": emp.name if emp else "",
                    "department": emp.department if emp else "",
                    "manager_emp_no": emp.manager_emp_no if emp else "",
                    "eval_status": e.status,
                    "what_self": e.what_self or "",
                    "what_manager": e.what_manager or "",
                    "what_final": e.what_final or "",
                    "how_self": e.how_self or "",
                    "how_manager": e.how_manager or "",
                    "how_final": e.how_final or "",
                    "self_comment": e.self_comment,
                    "manager_comment": e.manager_comment,
                    "hr_comment": e.hr_comment,
                    "updated_at": e.updated_at.isoformat(),
                }
            )
        st.download_button("evaluations.csv をダウンロード", data=to_csv(rows2).encode("utf-8"), file_name="evaluations.csv", mime="text/csv")


PAGES: Dict[str, Callable[[], None]] = {
    "login": page_login,
    "forgot_password": page_forgot_password,
    "password_change": page_password_change,
    "home": page_home,
    # employee
    "goal_input": page_goal_input,
    "goal_view_self": page_goal_view_self,
    "eval_input_self": page_eval_input_self,
    "eval_view_self": page_eval_view_self,
    "oneonone_employee": page_oneonone_employee,
    "approval_status_self": page_approval_status_self,
    # manager
    "goal_view_manager": page_goal_view_manager,
    "goal_approve_manager": page_goal_approve_manager,
    "eval_input_manager": page_eval_input_manager,
    "oneonone_manager": page_oneonone_manager,
    # admin
    "hr_dashboard": page_hr_dashboard,
    "admin_employee_master": page_admin_employee_master,
    "goal_approve_hr": page_goal_approve_hr,
    "eval_approve_hr": page_eval_approve_hr,
    "admin_csv": page_admin_csv,
}


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="expanded", menu_items=None)
    apply_custom_styles()
    init_db()

    user = get_auth()
    header_bar(user)
    nav_sidebar(user)

    page = get_page()
    if page not in PAGES:
        set_page("home" if user else "login")
        page = get_page()

    if user:
        _ = require_login()

    PAGES[page]()


if __name__ == "__main__":
    main()
