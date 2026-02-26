# FILE: app.py
"""
All-in-one Streamlit App: Goal -> Approval -> Evaluation -> 1on1 -> CSV
Streamlit Community Cloud + PostgreSQL

Key changes:
- 年度（year）を「ログイン時入力（4桁数字）」→ その後はセッション固定（全ページ共通）
- SQLite既存DBに対する軽量マイグレーション（employees.email 追加など）
- 初期管理者パスワード（admin / 土田）: ChangeMe_1234（Secrets未設定でもこの値）

Run:
pip install -r requirements.txt
streamlit run app.py

Secrets (Streamlit Community Cloud):
DATABASE_URL="postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME"
ADMIN_SEED_PASSWORD="ChangeMe_1234"
"""

from __future__ import annotations

import csv
import os
import secrets
import smtplib
from dataclasses import dataclass, asdict
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import StringIO
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

import matplotlib.pyplot as plt
import streamlit as st
from passlib.context import CryptContext
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

# ----------------------------
# Constants
# ----------------------------
APP_TITLE = "Goal Management System - Stanley Black & Decker"

PWD_CONTEXT = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], default="pbkdf2_sha256", deprecated="auto")

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

# ----------------------------
# Year (login input -> fixed in session)
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


def year_badge() -> None:
    st.caption(f"年度: {get_selected_year()}（固定）")


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
    # ★要件：ChangeMe_1234
    return get_secret("ADMIN_SEED_PASSWORD", "ChangeMe_1234") or "ChangeMe_1234"


# ----------------------------
# Email
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

    specific: Mapped[str] = mapped_column(Text, nullable=False)
    measurable: Mapped[str] = mapped_column(Text, nullable=False)
    achievable: Mapped[str] = mapped_column(Text, nullable=False)
    relevant: Mapped[str] = mapped_column(Text, nullable=False)
    time_bound: Mapped[str] = mapped_column(Text, nullable=False)

    career_vision: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    weight: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # business
    achieved_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # business

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    goal: Mapped[Goal] = relationship(back_populates="items")


class GoalApproval(Base):
    __tablename__ = "goal_approvals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    goal_id: Mapped[int] = mapped_column(Integer, ForeignKey("goals.id"), nullable=False)

    stage: Mapped[str] = mapped_column(String(16), nullable=False)  # employee/manager/hr
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # save/submit/approve/return
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
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # save/submit/approve/return
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
# DB init + SQLite migration
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
        def set_sqlite_pragma(dbapi_conn, connection_record):
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
    """
    Lightweight migration for SQLite only.
    - Add missing columns without dropping tables.
    """
    if not database_url().startswith("sqlite://"):
        return

    with ENGINE.begin() as conn:
        if not _sqlite_table_exists(conn, "employees"):
            return

        cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(employees)").fetchall()]
        if "email" not in cols:
            conn.exec_driver_sql("ALTER TABLE employees ADD COLUMN email VARCHAR(255)")


def init_db() -> None:
    Base.metadata.create_all(ENGINE)
    migrate_sqlite_schema_if_needed()
    seed_admin_if_needed()
    seed_how_questions_if_needed()

def seed_admin_if_needed() -> None:
    """
    Seed:
      - 000001 HR 管理者
      - 425025 土田光範
    Both: initial password = ChangeMe_1234 (or secrets ADMIN_SEED_PASSWORD)
    """
    pw = admin_seed_password()

    with SessionLocal() as db:
        admin = db.execute(select(Employee).where(Employee.emp_no == "000001")).scalar_one_or_none()
        if not admin:
            db.add(
                Employee(
                    emp_no="000001",
                    name="HR 管理者",
                    department="HR",
                    email="admin@company.example.com",
                    password_hash=PWD_CONTEXT.hash(pw),
                    active=True,
                    role_admin=True,
                    role_manager=False,
                    role_employee=False,
                    manager_emp_no=None,
                    must_change_password=True,
                    password_updated_at=None,
                    last_login_at=None,
                )
            )

        tsuchida = db.execute(select(Employee).where(Employee.emp_no == "425025")).scalar_one_or_none()
        if not tsuchida:
            db.add(
                Employee(
                    emp_no="425025",
                    name="土田光範",
                    department="HR",
                    email="tsuchida@company.example.com",
                    password_hash=PWD_CONTEXT.hash(pw),
                    active=True,
                    role_admin=True,
                    role_manager=False,
                    role_employee=False,
                    manager_emp_no=None,
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
    st.session_state.pop("selected_year", None)
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
# Domain helpers
# ----------------------------
def status_label_goal(status: str) -> str:
    return GOAL_STATUSES.get(status, status)


def status_label_eval(status: str) -> str:
    return EVAL_STATUSES.get(status, status)


def can_edit_goal(status: str) -> bool:
    return status in {"draft", "manager_returned", "hr_returned"}


def can_edit_eval_self(status: str) -> bool:
    return status in {"draft", "manager_returned", "hr_returned"}


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
# UI common
# ----------------------------
def section_title(title: str) -> None:
    st.subheader(title)
    year_badge()


# ----------------------------
# Pages: Auth
# ----------------------------
def page_login() -> None:
    st.title(APP_TITLE)
    with st.form("login_form", clear_on_submit=False):
        role: Role = st.selectbox("役割", ["HR管理者", "評価者", "入力者"])
        login_year = year_input_login(datetime.utcnow().year)
        emp_no = st.text_input("従業員番号", placeholder="例: 000001")
        password = st.text_input("パスワード", type="password")
        submitted = st.form_submit_button("ログイン")

    if not submitted:
        return

    emp_no = emp_no.strip()
    if not emp_no or not password:
        st.error("従業員番号とパスワードを入力してください。")
        return

    with SessionLocal() as db:
        emp = db.execute(select(Employee).where(Employee.emp_no == emp_no, Employee.active.is_(True))).scalar_one_or_none()
        if not emp or not verify_password(password, emp.password_hash):
            st.error("従業員番号またはパスワードが違います。")
            return

        role_flag = ROLE_TO_FLAG[role]
        if not bool(getattr(emp, role_flag)):
            st.error("この役割の権限がありません。")
            return

        emp.last_login_at = datetime.utcnow()
        db.add(emp)
        db.commit()

        set_selected_year(int(login_year), force=True)
        set_auth(AuthUser(emp_no=emp.emp_no, name=emp.name, role=role, role_key=ROLE_TO_KEY[role]))

        if emp.must_change_password:
            set_page("password_change")
        else:
            set_page("home")
        st.rerun()


def page_password_change() -> None:
    user = get_auth()
    if not user:
        set_page("login")
        st.stop()

    section_title("パスワード変更")

    with st.form("pw_change", clear_on_submit=True):
        current = st.text_input("現在のパスワード", type="password")
        new1 = st.text_input("新しいパスワード", type="password")
        new2 = st.text_input("新しいパスワード（確認）", type="password")
        ok = st.form_submit_button("変更")

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
# Minimal pages (home only) + Router
# ※ ここから先はあなたの既存ページ群を入れる想定です。
#   今回のエラー修正（email列追加）は上で完結しています。
# ----------------------------
def page_home() -> None:
    user = require_login()
    section_title("Home")
    st.write(f"ようこそ、{user.name} さん（{user.role}）")


PAGES: Dict[str, Callable[[], None]] = {
    "login": page_login,
    "password_change": page_password_change,
    "home": page_home,
}


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="expanded")
    init_db()

    page = get_page()
    if page not in PAGES:
        set_page("home" if get_auth() else "login")
        page = get_page()

    PAGES[page]()


if __name__ == "__main__":
    main()
