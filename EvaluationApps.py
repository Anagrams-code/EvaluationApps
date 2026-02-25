# FILE: app.py
"""
All-in-one Streamlit App: Goal -> Approval -> Evaluation -> 1on1 -> CSV
Streamlit Community Cloud + PostgreSQL

Applied changes:
1) Goals/Evaluations NOT split into mid/final; use Year only.
2) Employee master CSV upload (admin).
3) what thresholds: >=130% exceeds, >=95% meets.
4) how thresholds: based on total points ratio over 160 (40 questions * 4):
>=90% exceeds, >=60% meets.
5) HR dashboard with progress/ratios + filters (department, manager).
6) Manager subordinate goal view page (read-only).

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
from dataclasses import dataclass, asdict
from datetime import datetime
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
)
from sqlalchemy.orm import (
DeclarativeBase,
Mapped,
mapped_column,
sessionmaker,
relationship,
)

# ----------------------------
# Constants
# ----------------------------
APP_TITLE = "目標管理・評価・1on1（All-in-one）"
PWD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto")

Role = Literal["管理者", "評価者", "入力者"]
ROLE_TO_FLAG = {"管理者": "role_admin", "評価者": "role_manager", "入力者": "role_employee"}
ROLE_TO_KEY = {"管理者": "admin", "評価者": "manager", "入力者": "employee"}

GOAL_STATUSES = {
"draft": "下書き",
"submitted": "上長承認待ち",
"manager_returned": "上長差し戻し",
"manager_approved": "HR承認待ち",
"hr_returned": "HR差し戻し",
"hr_approved": "公開（確定）",
}

EVAL_STATUSES = {
"draft": "下書き",
"submitted_self": "上長評価待ち",
"manager_returned": "上長差し戻し",
"manager_submitted": "HR承認待ち",
"hr_returned": "HR差し戻し",
"hr_approved": "公開（確定）",
}

# ③ what判定（加重平均%）
WHAT_THRESHOLDS = {"exceeds": 130.0, "meets": 95.0}

# ④ how判定（得点率、満点160=40問×4点）
HOW_TOP = 4
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
# Secrets / Config
# ----------------------------
def get_secret(key: str, default: Optional[str] = None) -> Optional[str]:
if key in st.secrets:
return str(st.secrets[key])
return os.getenv(key, default)


def database_url() -> str:
url = get_secret("DATABASE_URL")
if not url:
os.makedirs("data", exist_ok=True)
return "sqlite:///data/app.db"
return url


def admin_seed_password() -> str:
return get_secret("ADMIN_SEED_PASSWORD", "admin1234") or "admin1234"


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
updated_at: Mapped[datetime] = mapped_column(
DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
)


class Goal(Base):
__tablename__ = "goals"
__table_args__ = (UniqueConstraint("employee_emp_no", "year", name="uq_goals_employee_year"),)

id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
employee_emp_no: Mapped[str] = mapped_column(String(32), ForeignKey("employees.emp_no"), nullable=False)
year: Mapped[int] = mapped_column(Integer, nullable=False)

status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)

created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
updated_at: Mapped[datetime] = mapped_column(
DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
)

items: Mapped[List["GoalItem"]] = relationship(back_populates="goal", cascade="all, delete-orphan")
approvals: Mapped[List["GoalApproval"]] = relationship(back_populates="goal", cascade="all, delete-orphan")


class GoalItem(Base):
__tablename__ = "goal_items"

id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
goal_id: Mapped[int] = mapped_column(Integer, ForeignKey("goals.id"), nullable=False)

type: Mapped[str] = mapped_column(String(16), nullable=False) # business/development

specific: Mapped[str] = mapped_column(Text, nullable=False)
measurable: Mapped[str] = mapped_column(Text, nullable=False)
achievable: Mapped[str] = mapped_column(Text, nullable=False)
relevant: Mapped[str] = mapped_column(Text, nullable=False)
time_bound: Mapped[str] = mapped_column(Text, nullable=False)

career_vision: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

weight: Mapped[Optional[int]] = mapped_column(Integer, nullable=True) # business only 0..100
achieved_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True) # business only 0..200

created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
updated_at: Mapped[datetime] = mapped_column(
DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
)

goal: Mapped[Goal] = relationship(back_populates="items")


class GoalApproval(Base):
__tablename__ = "goal_approvals"

id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
goal_id: Mapped[int] = mapped_column(Integer, ForeignKey("goals.id"), nullable=False)

stage: Mapped[str] = mapped_column(String(16), nullable=False) # employee/manager/hr
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
updated_at: Mapped[datetime] = mapped_column(
DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
)

answers: Mapped[List["HowAnswer"]] = relationship(back_populates="evaluation", cascade="all, delete-orphan")
approvals: Mapped[List["EvaluationApproval"]] = relationship(back_populates="evaluation", cascade="all, delete-orphan")
oneonone: Mapped[Optional["OneOnOne"]] = relationship(back_populates="evaluation", cascade="all, delete-orphan")


class HowQuestion(Base):
__tablename__ = "how_questions"
__table_args__ = (UniqueConstraint("category_key", "question_no", name="uq_howq_cat_no"),)

id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
category_key: Mapped[str] = mapped_column(String(64), nullable=False)
category_label: Mapped[str] = mapped_column(String(128), nullable=False)
question_no: Mapped[int] = mapped_column(Integer, nullable=False) # 1..5
question_text: Mapped[str] = mapped_column(Text, nullable=False)
active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class HowAnswer(Base):
__tablename__ = "how_answers"
__table_args__ = (UniqueConstraint("evaluation_id", "rater", "category_key", "question_no", name="uq_howa"),)

id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
evaluation_id: Mapped[int] = mapped_column(Integer, ForeignKey("evaluations.id"), nullable=False)
rater: Mapped[str] = mapped_column(String(16), nullable=False) # self/manager
category_key: Mapped[str] = mapped_column(String(64), nullable=False)
question_no: Mapped[int] = mapped_column(Integer, nullable=False)
score: Mapped[int] = mapped_column(Integer, nullable=False) # 1..4

evaluation: Mapped[Evaluation] = relationship(back_populates="answers")


class EvaluationApproval(Base):
__tablename__ = "evaluation_approvals"

id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
evaluation_id: Mapped[int] = mapped_column(Integer, ForeignKey("evaluations.id"), nullable=False)

stage: Mapped[str] = mapped_column(String(16), nullable=False) # self/manager/hr
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

status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False) # draft/proposed/confirmed

slot1: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
slot2: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
slot3: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

selected_slot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True) # 1..3
location: Mapped[str] = mapped_column(String(255), default="", nullable=False)
note: Mapped[str] = mapped_column(Text, default="", nullable=False)

created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
updated_at: Mapped[datetime] = mapped_column(
DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
)

evaluation: Mapped[Evaluation] = relationship(back_populates="oneonone")


# ----------------------------
# DB init
# ----------------------------
ENGINE = create_engine(database_url(), echo=False, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
Base.metadata.create_all(ENGINE)
seed_admin_if_needed()
seed_how_questions_if_needed()


def seed_admin_if_needed() -> None:
with SessionLocal() as db:
existing = db.execute(select(Employee).where(Employee.emp_no == "0001")).scalar_one_or_none()
if existing:
return
admin = Employee(
emp_no="0001",
name="HR 管理者（初期）",
department="HR",
password_hash=PWD_CONTEXT.hash(admin_seed_password()),
active=True,
role_admin=True,
role_manager=False,
role_employee=False,
manager_emp_no=None,
must_change_password=True,
password_updated_at=None,
last_login_at=None,
)
db.add(admin)
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

for cat_key, cat_label in HOW_CATEGORIES:
qs = templates[cat_key]
for i, text in enumerate(qs, start=1):
db.add(
HowQuestion(
category_key=cat_key,
category_label=cat_label,
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
"""
Returns (total, ratio, result) where:
total: sum scores (max 160 for 40 questions)
ratio: total / 160
result: exceeds/meets/does_not_meet
"""
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


def is_subordinate(db, manager_emp_no: str, employee_emp_no: str) -> bool:
emp = db.execute(select(Employee).where(Employee.emp_no == employee_emp_no)).scalar_one_or_none()
return bool(emp and emp.manager_emp_no == manager_emp_no)


def ensure_goal_exists_for_eval(db, employee_emp_no: str, year: int) -> Goal:
goal = db.execute(
select(Goal).where(Goal.employee_emp_no == employee_emp_no, Goal.year == year)
).scalar_one_or_none()
if not goal:
raise ValueError("先に目標を作成してください。")
if goal.status != "hr_approved":
raise ValueError("評価を開始するには、目標がHR承認（公開）されている必要があります。")
return goal


def get_or_create_evaluation(db, employee_emp_no: str, year: int, goal_id: int) -> Evaluation:
ev = db.execute(
select(Evaluation).where(Evaluation.employee_emp_no == employee_emp_no, Evaluation.year == year)
).scalar_one_or_none()
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
def header_bar(user: Optional[AuthUser]) -> None:
col1, col2 = st.columns([3, 1])
with col1:
st.title(APP_TITLE)
with col2:
if user:
st.caption(f"{user.name}（{user.role}）")
if st.button("ログアウト"):
logout()


def nav_sidebar(user: Optional[AuthUser]) -> None:
with st.sidebar:
st.subheader("ナビゲーション")

if not user:
st.caption("ログインしてください。")
return

if get_page() == "password_change":
st.info("パスワード変更が完了するまで操作できません。")
return

st.button("ホーム", on_click=set_page, args=("home",))
st.markdown("---")

if user.role == "入力者":
st.button("目標入力", on_click=set_page, args=("goal_input",))
st.button("目標閲覧", on_click=set_page, args=("goal_view_self",))
st.button("評価入力（自己）", on_click=set_page, args=("eval_input_self",))
st.button("評価閲覧", on_click=set_page, args=("eval_view_self",))
st.button("1on1閲覧/承認", on_click=set_page, args=("oneonone_employee",))
st.button("承認状況閲覧", on_click=set_page, args=("approval_status_self",))

if user.role == "評価者":
st.button("部下目標 閲覧（専用）", on_click=set_page, args=("goal_view_manager",)) # ⑥
st.button("部下目標 承認/差戻し", on_click=set_page, args=("goal_approve_manager",))
st.button("部下評価 入力/差戻し", on_click=set_page, args=("eval_input_manager",))
st.button("1on1設定", on_click=set_page, args=("oneonone_manager",))

if user.role == "管理者":
st.button("HRダッシュボード", on_click=set_page, args=("hr_dashboard",)) # ⑤
st.button("従業員マスタ", on_click=set_page, args=("admin_employee_master",))
st.button("目標 HR承認/差戻し", on_click=set_page, args=("goal_approve_hr",))
st.button("評価 HR承認/差戻し", on_click=set_page, args=("eval_approve_hr",))
st.button("CSV出力", on_click=set_page, args=("admin_csv",))

st.markdown("---")
st.button("パスワード変更", on_click=set_page, args=("password_change",))


# ----------------------------
# Pages: Auth
# ----------------------------
def page_login() -> None:
st.subheader("ログイン")

with st.form("login_form", clear_on_submit=False):
role: Role = st.selectbox("ロールを選択", ["管理者", "評価者", "入力者"])
emp_no = st.text_input("従業員番号", placeholder="例: 0001")
password = st.text_input("パスワード", type="password")
submitted = st.form_submit_button("ログイン")

col1, _ = st.columns([1, 3])
with col1:
if st.button("パスワードを忘れた"):
set_page("forgot_password")

if not submitted:
st.info("初期管理者: 従業員番号 0001 / 初期PWは Secrets の ADMIN_SEED_PASSWORD")
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
st.error("権限が割り当てられていません")
return

emp.last_login_at = datetime.utcnow()
db.add(emp)
db.commit()

auth = AuthUser(emp_no=emp.emp_no, name=emp.name, role=role, role_key=ROLE_TO_KEY[role])
set_auth(auth)

if emp.must_change_password:
set_page("password_change")
st.warning("初回ログインのため、パスワード変更が必要です。")
else:
set_page("home")
st.success("ログインしました。")


def page_forgot_password() -> None:
st.subheader("パスワードを忘れた")
st.info("管理者が一時パスワードを発行します。管理者へ連絡してください。")
if st.button("ログインに戻る"):
set_page("login")


def page_password_change() -> None:
user = get_auth()
if not user:
set_page("login")
st.stop()

st.subheader("パスワード変更（必須）")

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


# ----------------------------
# Pages: Home
# ----------------------------
def page_home() -> None:
user = require_login()
st.subheader("ホーム")
st.write("操作を選択してください。")

if user.role == "入力者":
c1, c2 = st.columns(2)
with c1:
st.button("目標入力", use_container_width=True, on_click=set_page, args=("goal_input",))
st.button("評価入力（自己）", use_container_width=True, on_click=set_page, args=("eval_input_self",))
st.button("承認状況閲覧", use_container_width=True, on_click=set_page, args=("approval_status_self",))
with c2:
st.button("目標閲覧", use_container_width=True, on_click=set_page, args=("goal_view_self",))
st.button("評価閲覧", use_container_width=True, on_click=set_page, args=("eval_view_self",))
st.button("1on1閲覧/承認", use_container_width=True, on_click=set_page, args=("oneonone_employee",))

if user.role == "評価者":
c1, c2 = st.columns(2)
with c1:
st.button("部下目標 閲覧（専用）", use_container_width=True, on_click=set_page, args=("goal_view_manager",))
st.button("部下目標 承認/差戻し", use_container_width=True, on_click=set_page, args=("goal_approve_manager",))
with c2:
st.button("部下評価 入力/差戻し", use_container_width=True, on_click=set_page, args=("eval_input_manager",))
st.button("1on1設定", use_container_width=True, on_click=set_page, args=("oneonone_manager",))

if user.role == "管理者":
c1, c2 = st.columns(2)
with c1:
st.button("HRダッシュボード", use_container_width=True, on_click=set_page, args=("hr_dashboard",))
st.button("従業員マスタ", use_container_width=True, on_click=set_page, args=("admin_employee_master",))
with c2:
st.button("目標 HR承認/差戻し", use_container_width=True, on_click=set_page, args=("goal_approve_hr",))
st.button("評価 HR承認/差戻し", use_container_width=True, on_click=set_page, args=("eval_approve_hr",))
st.button("CSV出力", use_container_width=True, on_click=set_page, args=("admin_csv",))


# ----------------------------
# Admin: Employee Master + Reset + CSV Upload (②)
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
"emp_no,name,department,active,role_admin,role_manager,role_employee,manager_emp_no,password\n"
"0002,山田太郎,Sales,1,0,1,1,0100,TempPass_1234\n"
"0100,佐藤花子,Sales,1,0,1,0,,TempPass_5678\n"
)


def page_admin_employee_master() -> None:
require_role("管理者")
st.subheader("従業員マスタ（管理者）")

st.markdown("### CSVアップロード（②）")
st.caption("列: emp_no,name,department,active,role_admin,role_manager,role_employee,manager_emp_no,password（passwordは任意）")
st.download_button(
"CSVテンプレートをダウンロード",
data=_csv_template_text().encode("utf-8"),
file_name="employee_master_template.csv",
mime="text/csv",
)

up = st.file_uploader("従業員マスタCSVをアップロード", type=["csv"])
if up is not None:
raw = up.getvalue().decode("utf-8-sig")
reader = csv.DictReader(StringIO(raw))
required_cols = {"emp_no", "name"}
missing = [c for c in required_cols if c not in (reader.fieldnames or [])]
if missing:
st.error(f"必須列が足りません: {missing}")
else:
created_pw: List[Dict[str, str]] = []
updated_count = 0
created_count = 0
with SessionLocal() as db:
for row in reader:
emp_no = str(row.get("emp_no", "")).strip()
name = str(row.get("name", "")).strip()
if not emp_no or not name:
continue

department = str(row.get("department", "") or "").strip()
active = _parse_bool(row.get("active", "1"), True)
role_admin = _parse_bool(row.get("role_admin", "0"), False)
role_manager = _parse_bool(row.get("role_manager", "0"), False)
role_employee = _parse_bool(row.get("role_employee", "1"), True)
manager_emp_no = str(row.get("manager_emp_no", "") or "").strip() or None
password = str(row.get("password", "") or "").strip()

existing = db.execute(select(Employee).where(Employee.emp_no == emp_no)).scalar_one_or_none()
if existing:
existing.name = name
existing.department = department
existing.active = active
existing.role_admin = role_admin
existing.role_manager = role_manager
existing.role_employee = role_employee
existing.manager_emp_no = manager_emp_no
if password:
existing.password_hash = PWD_CONTEXT.hash(password)
existing.must_change_password = True
existing.password_updated_at = None
db.add(existing)
updated_count += 1
else:
if not password:
password = _generate_temp_password()
created_pw.append({"emp_no": emp_no, "name": name, "temporary_password": password})
emp = Employee(
emp_no=emp_no,
name=name,
department=department,
password_hash=PWD_CONTEXT.hash(password),
active=active,
role_admin=role_admin,
role_manager=role_manager,
role_employee=role_employee,
manager_emp_no=manager_emp_no,
must_change_password=True,
password_updated_at=None,
last_login_at=None,
)
db.add(emp)
created_count += 1
db.commit()

st.success(f"CSV取り込み完了：新規 {created_count} / 更新 {updated_count}")
if created_pw:
st.warning("password列が空だった新規ユーザーに一時パスワードを発行しました（必ず控えてください）。")
st.dataframe(created_pw, use_container_width=True)
st.download_button(
"一時パスワード一覧をダウンロード",
data=to_csv(created_pw).encode("utf-8"),
file_name="temporary_passwords.csv",
mime="text/csv",
)

st.markdown("---")
st.markdown("### 画面で追加/更新（手動）")

with st.form("emp_upsert", clear_on_submit=True):
emp_no = st.text_input("従業員番号", placeholder="例: 0002")
name = st.text_input("氏名", placeholder="例: 山田 太郎")
department = st.text_input("部署", placeholder="例: Sales")
password = st.text_input("初期/変更パスワード（空なら変更しない）", type="password")
active = st.checkbox("在籍", value=True)

col1, col2, col3 = st.columns(3)
with col1:
role_admin = st.checkbox("管理者権限")
with col2:
role_manager = st.checkbox("評価者権限")
with col3:
role_employee = st.checkbox("入力者権限", value=True)

manager_emp_no = st.text_input("上長の従業員番号（任意）", placeholder="例: 0100（評価者）")
ok = st.form_submit_button("保存")

if ok:
emp_no = emp_no.strip()
if not emp_no or not name.strip():
st.error("従業員番号と氏名は必須です。")
elif not (role_admin or role_manager or role_employee):
st.error("少なくとも1つの権限を付与してください。")
else:
with SessionLocal() as db:
existing = db.execute(select(Employee).where(Employee.emp_no == emp_no)).scalar_one_or_none()
if existing:
existing.name = name.strip()
existing.department = department.strip()
existing.active = bool(active)
existing.role_admin = bool(role_admin)
existing.role_manager = bool(role_manager)
existing.role_employee = bool(role_employee)
existing.manager_emp_no = manager_emp_no.strip() or None
if password:
existing.password_hash = PWD_CONTEXT.hash(password)
existing.must_change_password = True
existing.password_updated_at = None
db.add(existing)
db.commit()
st.success("更新しました。")
else:
if not password:
st.error("新規追加の場合、初期パスワードは必須です。")
else:
emp = Employee(
emp_no=emp_no,
name=name.strip(),
department=department.strip(),
password_hash=PWD_CONTEXT.hash(password),
active=bool(active),
role_admin=bool(role_admin),
role_manager=bool(role_manager),
role_employee=bool(role_employee),
manager_emp_no=manager_emp_no.strip() or None,
must_change_password=True,
password_updated_at=None,
last_login_at=None,
)
db.add(emp)
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
with SessionLocal() as db:
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


# ----------------------------
# Goals (Employee)
# ----------------------------
def _default_business_row() -> Dict[str, Any]:
return {
"S（Specific）": "",
"M（Measurable）": "",
"A（Achievable）": "",
"R（Relevant）": "",
"T（Time-bound）": "",
"Weight(0-100)": 100,
"達成率%(0-200)": 0,
}


def _default_development_row() -> Dict[str, Any]:
return {
"キャリアビジョン": "",
"S（Specific）": "",
"M（Measurable）": "",
"A（Achievable）": "",
"R（Relevant）": "",
"T（Time-bound）": "",
}


def validate_goal_rows(biz_rows: List[Dict[str, Any]], dev_rows: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
errors: List[str] = []

def nonempty(v: Any) -> bool:
return bool(str(v).strip())

if len(biz_rows) == 0 and len(dev_rows) == 0:
errors.append("business goal または development goal を1つ以上入力してください。")

for i, r in enumerate(biz_rows, start=1):
for k in ["S（Specific）", "M（Measurable）", "A（Achievable）", "R（Relevant）", "T（Time-bound）"]:
if not nonempty(r.get(k, "")):
errors.append(f"business goal #{i}: {k} は必須です。")
try:
w = int(r.get("Weight(0-100)", 0))
if w < 0 or w > 100:
errors.append(f"business goal #{i}: Weight は 0〜100 です。")
except Exception:
errors.append(f"business goal #{i}: Weight は数値で入力してください。")
try:
p = int(r.get("達成率%(0-200)", 0))
if p < 0 or p > 200:
errors.append(f"business goal #{i}: 達成率% は 0〜200 です。")
except Exception:
errors.append(f"business goal #{i}: 達成率% は数値で入力してください。")

wsum = 0
for r in biz_rows:
try:
wsum += int(r.get("Weight(0-100)", 0))
except Exception:
pass
if biz_rows and wsum != 100:
errors.append(f"business goal の Weight 合計が 100 ではありません（現在: {wsum}）。")

for i, r in enumerate(dev_rows, start=1):
if not nonempty(r.get("キャリアビジョン", "")):
errors.append(f"development goal #{i}: キャリアビジョン は必須です。")
for k in ["S（Specific）", "M（Measurable）", "A（Achievable）", "R（Relevant）", "T（Time-bound）"]:
if not nonempty(r.get(k, "")):
errors.append(f"development goal #{i}: {k} は必須です。")

return (len(errors) == 0), errors


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


def page_goal_input() -> None:
user = require_role("入力者")
st.subheader("目標入力（SMART）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1))

with SessionLocal() as db:
emp = db.execute(select(Employee).where(Employee.emp_no == user.emp_no)).scalar_one()
if not emp.manager_emp_no:
st.warning("従業員マスタで上長が設定されていません。Submitできません。")

goal = load_or_create_goal(db, user.emp_no, year)
st.caption(f"状態: {status_label_goal(goal.status)}")
editable = can_edit_goal(goal.status)

state_key = f"goal:{user.emp_no}:{year}"
if state_key not in st.session_state:
biz_rows: List[Dict[str, Any]] = []
dev_rows: List[Dict[str, Any]] = []
for it in goal.items:
if it.type == "business":
biz_rows.append(
{
"S（Specific）": it.specific,
"M（Measurable）": it.measurable,
"A（Achievable）": it.achievable,
"R（Relevant）": it.relevant,
"T（Time-bound）": it.time_bound,
"Weight(0-100)": it.weight or 0,
"達成率%(0-200)": it.achieved_percent or 0,
}
)
else:
dev_rows.append(
{
"キャリアビジョン": it.career_vision or "",
"S（Specific）": it.specific,
"M（Measurable）": it.measurable,
"A（Achievable）": it.achievable,
"R（Relevant）": it.relevant,
"T（Time-bound）": it.time_bound,
}
)
if not biz_rows:
biz_rows = [_default_business_row()]
if dev_rows is None:
dev_rows = []
st.session_state[state_key] = {"biz": biz_rows, "dev": dev_rows}

st.markdown("### business goal（評価対象）")
st.caption("SMART＋Weight（合計100）＋達成率（評価時にwhat計算に使用）")
biz_rows = st.data_editor(
st.session_state[state_key]["biz"],
num_rows="dynamic" if editable else "fixed",
use_container_width=True,
disabled=not editable,
)

st.markdown("### development goal（キャリア）")
st.caption("キャリアビジョン必須")
dev_rows = st.data_editor(
st.session_state[state_key]["dev"],
num_rows="dynamic" if editable else "fixed",
use_container_width=True,
disabled=not editable,
)

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
def row_nonempty(r: Dict[str, Any], keys: List[str]) -> bool:
return any(str(r.get(k, "")).strip() for k in keys)

biz_trim = [
r for r in biz_rows
if row_nonempty(r, ["S（Specific）", "M（Measurable）", "A（Achievable）", "R（Relevant）", "T（Time-bound）"])
]
dev_trim = [
r for r in dev_rows
if row_nonempty(r, ["キャリアビジョン", "S（Specific）", "M（Measurable）", "A（Achievable）", "R（Relevant）", "T（Time-bound）"])
]

ok, errors = validate_goal_rows(biz_trim, dev_trim)
if not ok:
st.error("入力に不備があります。")
for e in errors:
st.write(f"- {e}")
st.stop()

if submit and not emp.manager_emp_no:
st.error("上長が設定されていないためSubmitできません。")
st.stop()

goal = load_or_create_goal(db, user.emp_no, year)
if not can_edit_goal(goal.status):
st.error("この目標は編集できない状態です。")
st.stop()

for it in list(goal.items):
db.delete(it)
db.flush()

for r in biz_trim:
db.add(
GoalItem(
goal_id=goal.id,
type="business",
specific=str(r["S（Specific）"]).strip(),
measurable=str(r["M（Measurable）"]).strip(),
achievable=str(r["A（Achievable）"]).strip(),
relevant=str(r["R（Relevant）"]).strip(),
time_bound=str(r["T（Time-bound）"]).strip(),
career_vision=None,
weight=int(r["Weight(0-100)"]),
achieved_percent=int(r.get("達成率%(0-200)", 0)),
)
)

for r in dev_trim:
db.add(
GoalItem(
goal_id=goal.id,
type="development",
specific=str(r["S（Specific）"]).strip(),
measurable=str(r["M（Measurable）"]).strip(),
achievable=str(r["A（Achievable）"]).strip(),
relevant=str(r["R（Relevant）"]).strip(),
time_bound=str(r["T（Time-bound）"]).strip(),
career_vision=str(r["キャリアビジョン"]).strip(),
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

db.add(goal)
db.commit()
st.success("保存しました。" if save else "上長へ承認依頼しました。")
st.session_state.pop(state_key, None)
st.rerun()


def page_goal_view_self() -> None:
user = require_role("入力者")
st.subheader("目標閲覧（自分）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1, key="gvs_year"))

with SessionLocal() as db:
goal = db.execute(select(Goal).where(Goal.employee_emp_no == user.emp_no, Goal.year == year)).scalar_one_or_none()
if not goal:
st.info("この年度の目標はまだありません。")
return

st.caption(f"状態: {status_label_goal(goal.status)}")

items = db.execute(select(GoalItem).where(GoalItem.goal_id == goal.id).order_by(GoalItem.id.asc())).scalars().all()
approvals = db.execute(select(GoalApproval).where(GoalApproval.goal_id == goal.id).order_by(GoalApproval.acted_at.asc())).scalars().all()

biz = [it for it in items if it.type == "business"]
dev = [it for it in items if it.type == "development"]

st.markdown("### business goal")
for idx, it in enumerate(biz, start=1):
with st.expander(f"business #{idx}（Weight {it.weight} / 達成率 {it.achieved_percent}%）"):
st.write(f"**S**: {it.specific}")
st.write(f"**M**: {it.measurable}")
st.write(f"**A**: {it.achievable}")
st.write(f"**R**: {it.relevant}")
st.write(f"**T**: {it.time_bound}")

st.markdown("### development goal")
for idx, it in enumerate(dev, start=1):
with st.expander(f"development #{idx}"):
st.write(f"**キャリアビジョン**: {it.career_vision}")
st.write(f"**S**: {it.specific}")
st.write(f"**M**: {it.measurable}")
st.write(f"**A**: {it.achievable}")
st.write(f"**R**: {it.relevant}")
st.write(f"**T**: {it.time_bound}")

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


# ----------------------------
# ⑥ Manager: Subordinate Goal View (read-only)
# ----------------------------
def page_goal_view_manager() -> None:
user = require_role("評価者")
st.subheader("部下目標 閲覧（上長・閲覧専用）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1, key="gvm_year"))

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

st.caption(f"状態: {status_label_goal(goal.status)}")
items = db.execute(select(GoalItem).where(GoalItem.goal_id == goal.id).order_by(GoalItem.id.asc())).scalars().all()

biz = [it for it in items if it.type == "business"]
dev = [it for it in items if it.type == "development"]

st.markdown("### business goal")
if not biz:
st.write("（なし）")
for idx, it in enumerate(biz, start=1):
with st.expander(f"business #{idx}（Weight {it.weight} / 達成率 {it.achieved_percent}%）"):
st.write(f"**S**: {it.specific}")
st.write(f"**M**: {it.measurable}")
st.write(f"**A**: {it.achievable}")
st.write(f"**R**: {it.relevant}")
st.write(f"**T**: {it.time_bound}")

st.markdown("### development goal")
if not dev:
st.write("（なし）")
for idx, it in enumerate(dev, start=1):
with st.expander(f"development #{idx}"):
st.write(f"**キャリアビジョン**: {it.career_vision}")
st.write(f"**S**: {it.specific}")
st.write(f"**M**: {it.measurable}")
st.write(f"**A**: {it.achievable}")
st.write(f"**R**: {it.relevant}")
st.write(f"**T**: {it.time_bound}")


# ----------------------------
# Goals Approval (Manager / HR)
# ----------------------------
def page_goal_approve_manager() -> None:
user = require_role("評価者")
st.subheader("部下目標 承認/差戻し（上長）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1, key="gm_year"))

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

st.caption(f"状態: {status_label_goal(goal.status)}")
items = db.execute(select(GoalItem).where(GoalItem.goal_id == goal.id).order_by(GoalItem.id.asc())).scalars().all()

st.markdown("### 目標内容")
for it in items:
title = f"{it.type}（Weight {it.weight}）" if it.type == "business" else "development"
with st.expander(title):
if it.type == "development":
st.write(f"**キャリアビジョン**: {it.career_vision}")
st.write(f"**S**: {it.specific}")
st.write(f"**M**: {it.measurable}")
st.write(f"**A**: {it.achievable}")
st.write(f"**R**: {it.relevant}")
st.write(f"**T**: {it.time_bound}")

if goal.status != "submitted":
st.info("この目標は上長承認待ちではありません。")
return

st.markdown("---")
comment = st.text_area("コメント（差し戻し時は必須）", key="gm_comment")
c1, c2 = st.columns(2)
with c1:
approve = st.button("承認（HRへ提出）", use_container_width=True)
with c2:
ret = st.button("差し戻し", use_container_width=True)

if approve:
goal.status = "manager_approved"
db.add(goal)
db.add(GoalApproval(goal_id=goal.id, stage="manager", action="approve", comment=comment.strip(), actor_emp_no=user.emp_no))
db.commit()
st.success("承認しました（HRへ提出）。")
st.rerun()

if ret:
if not comment.strip():
st.error("差し戻し時はコメントが必須です。")
st.stop()
goal.status = "manager_returned"
db.add(goal)
db.add(GoalApproval(goal_id=goal.id, stage="manager", action="return", comment=comment.strip(), actor_emp_no=user.emp_no))
db.commit()
st.warning("差し戻しました。")
st.rerun()


def page_goal_approve_hr() -> None:
user = require_role("管理者")
st.subheader("目標 HR承認/差戻し（管理者）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1, key="gh_year"))

with SessionLocal() as db:
candidates = db.execute(
select(Goal).where(Goal.year == year, Goal.status == "manager_approved").order_by(Goal.updated_at.desc())
).scalars().all()
if not candidates:
st.info("HR承認待ちの目標がありません。")
return

options = {}
for g in candidates:
emp = db.execute(select(Employee).where(Employee.emp_no == g.employee_emp_no)).scalar_one_or_none()
options[f"{g.employee_emp_no} {emp.name if emp else ''}（更新 {g.updated_at:%Y-%m-%d}）"] = g.id

label = st.selectbox("対象を選択", list(options.keys()))
goal_id = options[label]

goal = db.execute(select(Goal).where(Goal.id == goal_id)).scalar_one()
items = db.execute(select(GoalItem).where(GoalItem.goal_id == goal.id).order_by(GoalItem.id.asc())).scalars().all()

st.caption(f"状態: {status_label_goal(goal.status)}")
st.markdown("### 目標内容")
for it in items:
title = f"{it.type}（Weight {it.weight}）" if it.type == "business" else "development"
with st.expander(title):
if it.type == "development":
st.write(f"**キャリアビジョン**: {it.career_vision}")
st.write(f"**S**: {it.specific}")
st.write(f"**M**: {it.measurable}")
st.write(f"**A**: {it.achievable}")
st.write(f"**R**: {it.relevant}")
st.write(f"**T**: {it.time_bound}")

comment = st.text_area("コメント（差し戻し時は必須）", key="gh_comment")
c1, c2 = st.columns(2)
with c1:
approve = st.button("HR承認（公開）", use_container_width=True)
with c2:
ret = st.button("差し戻し（社員へ）", use_container_width=True)

if approve:
goal.status = "hr_approved"
db.add(goal)
db.add(GoalApproval(goal_id=goal.id, stage="hr", action="approve", comment=comment.strip(), actor_emp_no=user.emp_no))
db.commit()
st.success("HR承認しました（公開）。")
st.rerun()

if ret:
if not comment.strip():
st.error("差し戻し時はコメントが必須です。")
st.stop()
goal.status = "hr_returned"
db.add(goal)
db.add(GoalApproval(goal_id=goal.id, stage="hr", action="return", comment=comment.strip(), actor_emp_no=user.emp_no))
db.commit()
st.warning("差し戻しました（社員が修正→再Submit）。")
st.rerun()


# ----------------------------
# Evaluation (Self / Manager / HR) + View
# ----------------------------
def page_eval_input_self() -> None:
user = require_role("入力者")
st.subheader("評価入力（自己）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1, key="es_year"))

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
"S（Specific）": it.specific[:50] + ("…" if len(it.specific) > 50 else ""),
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

pct2, what2 = calc_what_from_business(biz)
total2, ratio2, how2 = calc_how_from_scores(scores)

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
st.rerun()


def page_eval_input_manager() -> None:
user = require_role("評価者")
st.subheader("部下評価 入力/差戻し（上長）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1, key="em_year"))

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
st.success("保存しました。" if save else ("提出しました（HR承認待ち）。" if submit else "差し戻しました。"))
st.rerun()


def page_eval_approve_hr() -> None:
user = require_role("管理者")
st.subheader("評価 HR承認/差戻し（管理者）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1, key="eh_year"))

with SessionLocal() as db:
candidates = db.execute(
select(Evaluation).where(Evaluation.year == year, Evaluation.status == "manager_submitted").order_by(Evaluation.updated_at.desc())
).scalars().all()
if not candidates:
st.info("HR承認待ちの評価がありません。")
return

options = {}
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
st.caption(f"状態: {status_label_eval(ev.status)}")
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
approve = st.button("HR承認（確定/公開）", use_container_width=True)
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
st.success("HR承認しました。上長は1on1設定ができます。")
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
st.subheader("評価閲覧（自分）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1, key="evs_year"))

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


# ----------------------------
# 1on1 (Manager / Employee)
# ----------------------------
def page_oneonone_manager() -> None:
user = require_role("評価者")
st.subheader("1on1設定（上長）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1, key="o1m_year"))

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
st.info("HR承認済み評価がありません（HR承認後に1on1設定できます）。")
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
st.subheader("1on1閲覧/承認（社員）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1, key="o1e_year"))

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
if o1.slot1: slots.append((1, o1.slot1))
if o1.slot2: slots.append((2, o1.slot2))
if o1.slot3: slots.append((3, o1.slot3))

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
st.rerun()


# ----------------------------
# Approval status (Employee)
# ----------------------------
def page_approval_status_self() -> None:
user = require_role("入力者")
st.subheader("承認状況（自分）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1))

with SessionLocal() as db:
goal = db.execute(select(Goal).where(Goal.employee_emp_no == user.emp_no, Goal.year == year)).scalar_one_or_none()
ev = db.execute(select(Evaluation).where(Evaluation.employee_emp_no == user.emp_no, Evaluation.year == year)).scalar_one_or_none()

st.markdown("### 目標")
if not goal:
st.write("未作成")
else:
st.write(f"状態: {status_label_goal(goal.status)}")
approvals = db.execute(select(GoalApproval).where(GoalApproval.goal_id == goal.id).order_by(GoalApproval.acted_at.asc())).scalars().all()
rows = [{
"日時": a.acted_at.strftime("%Y-%m-%d %H:%M"),
"ステージ": a.stage,
"アクション": a.action,
"コメント": (a.comment or "").strip(),
} for a in approvals]
st.dataframe(rows, use_container_width=True)

st.markdown("### 評価")
if not ev:
st.write("未開始")
else:
st.write(f"状態: {status_label_eval(ev.status)}")
approvals = db.execute(select(EvaluationApproval).where(EvaluationApproval.evaluation_id == ev.id).order_by(EvaluationApproval.acted_at.asc())).scalars().all()
rows = [{
"日時": a.acted_at.strftime("%Y-%m-%d %H:%M"),
"ステージ": a.stage,
"アクション": a.action,
"コメント": (a.comment or "").strip(),
} for a in approvals]
st.dataframe(rows, use_container_width=True)


# ----------------------------
# ⑤ HR Dashboard (filters by department / manager)
# ----------------------------
def page_hr_dashboard() -> None:
require_role("管理者")
st.subheader("HRダッシュボード（進捗・割合・フィルタ）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1, key="hrd_year"))

with SessionLocal() as db:
employees = db.execute(select(Employee).where(Employee.active.is_(True))).scalars().all()
managers = [e for e in employees if e.role_manager]
departments = sorted({e.department for e in employees if e.department})

dept = st.selectbox("部署で絞り込み", options=["(全て)"] + departments)
mgr_opts = {"(全て)": None}
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

# progress metrics
st.markdown("### 対象人数")
st.metric("入力者（対象）", value=str(len(filtered_emps)))

# Goal status counts
goal_counts = {k: 0 for k in GOAL_STATUSES.keys()}
for g in goals_f:
goal_counts[g.status] = goal_counts.get(g.status, 0) + 1

# Evaluation status counts
eval_counts = {k: 0 for k in EVAL_STATUSES.keys()}
for e in evals_f:
eval_counts[e.status] = eval_counts.get(e.status, 0) + 1

c1, c2 = st.columns(2)
with c1:
st.markdown("### 目標 進捗（件数）")
rows = [{"status": GOAL_STATUSES[k], "count": goal_counts.get(k, 0)} for k in GOAL_STATUSES.keys()]
st.dataframe(rows, use_container_width=True)
with c2:
st.markdown("### 評価 進捗（件数）")
rows = [{"status": EVAL_STATUSES[k], "count": eval_counts.get(k, 0)} for k in EVAL_STATUSES.keys()]
st.dataframe(rows, use_container_width=True)

# ratios
def ratio(n: int, d: int) -> float:
return (n / d * 100.0) if d else 0.0

total_emp = len(filtered_emps)
goal_created = len({g.employee_emp_no for g in goals_f})
goal_submitted_or_more = len({g.employee_emp_no for g in goals_f if g.status in {"submitted","manager_approved","manager_returned","hr_returned","hr_approved"}})
goal_hr_approved = len({g.employee_emp_no for g in goals_f if g.status == "hr_approved"})

eval_started = len({e.employee_emp_no for e in evals_f})
eval_manager_submitted = len({e.employee_emp_no for e in evals_f if e.status in {"manager_submitted","hr_returned","hr_approved"}})
eval_hr_approved = len({e.employee_emp_no for e in evals_f if e.status == "hr_approved"})

st.markdown("### 進捗割合（対象人数に対する比率）")
r1, r2, r3 = st.columns(3)
r1.metric("目標作成率", f"{ratio(goal_created, total_emp):.1f}%")
r2.metric("目標提出率", f"{ratio(goal_submitted_or_more, total_emp):.1f}%")
r3.metric("目標HR承認率", f"{ratio(goal_hr_approved, total_emp):.1f}%")

r4, r5, r6 = st.columns(3)
r4.metric("評価開始率", f"{ratio(eval_started, total_emp):.1f}%")
r5.metric("評価上長提出率", f"{ratio(eval_manager_submitted, total_emp):.1f}%")
r6.metric("評価HR承認率", f"{ratio(eval_hr_approved, total_emp):.1f}%")

st.markdown("---")
st.markdown("### 一覧（フィルタ結果）")
rows = []
goal_map = {(g.employee_emp_no): g for g in goals_f}
eval_map = {(e.employee_emp_no): e for e in evals_f}
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


# ----------------------------
# Admin CSV Export
# ----------------------------
def page_admin_csv() -> None:
require_role("管理者")
st.subheader("CSV出力（管理者）")

now = datetime.utcnow()
year = int(st.number_input("年度", min_value=2000, max_value=2100, value=int(now.year), step=1, key="csv_year"))

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
"career_vision": it.career_vision or "",
"weight": it.weight if it.type == "business" else "",
"achieved_percent": it.achieved_percent if it.type == "business" else "",
"updated_at": g.updated_at.isoformat(),
}
)
goal_csv = to_csv(rows)
st.download_button("goals.csv をダウンロード", data=goal_csv.encode("utf-8"), file_name="goals.csv", mime="text/csv")

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
eval_csv = to_csv(rows2)
st.download_button("evaluations.csv をダウンロード", data=eval_csv.encode("utf-8"), file_name="evaluations.csv", mime="text/csv")


# ----------------------------
# Router
# ----------------------------
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
"goal_view_manager": page_goal_view_manager, # ⑥
"goal_approve_manager": page_goal_approve_manager,
"eval_input_manager": page_eval_input_manager,
"oneonone_manager": page_oneonone_manager,

# admin
"hr_dashboard": page_hr_dashboard, # ⑤
"admin_employee_master": page_admin_employee_master, # ②
"goal_approve_hr": page_goal_approve_hr,
"eval_approve_hr": page_eval_approve_hr,
"admin_csv": page_admin_csv,
}


def main() -> None:
st.set_page_config(page_title=APP_TITLE, layout="wide")
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