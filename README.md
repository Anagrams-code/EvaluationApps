# Goal Management System - Stanley Black & Decker Edition

モダンなUIとStanley Black&Deckerのブランドカラーを実装した、エンタープライズグレードの目標管理・評価システムです。

## 🎨 ブランドカラー

- **プライマリカラー**: `#FFCC00` (Stanley Yellow)
- **セカンダリカラー**: `#000000` (Black)
- **背景色**: `#F5F5F5` (Light Gray)
- **テキスト色**: `#333333` (Dark Gray)

## 📋 機能

### Employee (社員)
- 📝 **Goal Input**: SMARTフレームワークに基づいた目標入力
- 👁️ **View Goals**: 自分の目標を閲覧
- ⭐ **Self Evaluation**: 自己評価入力
- 📋 **View Evaluations**: 自分の評価結果を閲覧
- 🗣️ **1:1 Review**: 1対1面談の日程確認と承認
- 📊 **Approval Status**: 承認プロセスの進捗確認

### Manager (評価者)
- 📂 **View Team Goals**: 部下の目標を閲覧（読み取り専用）
- ✅ **Approve Goals**: 部下の目標を承認・差戻し
- ⭐ **Evaluate Team**: 部下を評価
- 📅 **Schedule 1:1**: 1対1面談の日程設定

### Admin (管理者)
- 📈 **HR Dashboard**: 進捗状況と統計分析
- 👥 **Employee Master**: 従業員データの管理
- ✔️ **Approve Goals (HR)**: 目標のHR承認
- ✔️ **Approve Evaluations (HR)**: 評価のHR承認
- 📥 **Export CSV**: データのエクスポート

## 🚀 セットアップ

### 前提条件
- Python 3.9+
- PostgreSQL (オプション、SQLiteもサポート)
- Streamlit

### インストール

```bash
# 1. 依存パッケージをインストール
pip install -r requirements.txt

# 2. 環境変数を設定 (Streamlit Community Cloud の場合)
# DATABASE_URL と ADMIN_SEED_PASSWORD をシークレットに追加
```

### ローカル実行

```bash
# 標準的な実行方法
streamlit run EvaluationApps.py

# カスタムポート設定
streamlit run EvaluationApps.py --server.port 8501
```

ブラウザで `http://localhost:8501` にアクセスしてください。

## 🔐 デフォルトアカウント

初期管理者アカウント:
- **Employee ID**: `admin`
- **Password**: `.env` の `ADMIN_SEED_PASSWORD` で指定

> 初回ログイン時は、パスワードの変更が必須です。

## 📊 評価ロジック

### What判定（目標達成度）
- **Exceeds** (優秀): ≥ 130%
- **Meets** (達成): ≥ 95%
- **Does Not Meet** (未達成): < 95%

### How判定（行動評価）
- **Exceeds** (優秀): ≥ 90%
- **Meets** (達成): ≥ 60%
- **Does Not Meet** (未達成): < 60%

評価は8つのカテゴリーで実施：
1. 革新さと勇気
2. 機敏さとパフォーマンス
3. 包括性とコラボレーション
4. 誠実さ
5. 顧客焦点
6. 他者への影響
7. 変革リーダーシップ
8. 効率性

## 📁 ファイル構成

```
EvaluationApps/
├── EvaluationApps.py              # メインアプリケーション
├── requirements.txt               # Python依存パッケージ
├── .streamlit/
│   └── config.toml               # Streamlit設定（ブランドカラー）
├── data/                         # データディレクトリ
└── README.md                     # このファイル
```

## 🎨 カスタマイズ

### テーマカラーの変更

`.streamlit/config.toml` で色定義を変更:

```toml
[theme]
primaryColor = "#FFCC00"              # メインカラー
backgroundColor = "#FFFFFF"           # 背景色
secondaryBackgroundColor = "#F5F5F5"  # サイドバー背景
textColor = "#333333"                 # テキスト色
```

### セクションタイトルの使用

カスタムセクションタイトルを使用:

```python
from EvaluationApps import section_title

section_title("My Section Title", "📌")
```

## 🗄️ データベース

### SQLiteの場合（デフォルト）
データは `data/eval.db` に自動保存されます。

### PostgreSQLの場合
環境変数で接続情報を指定:
```
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/eval_db
```

## 📝 CSV出力フォーマット

### 目標CSV
- Employee ID, Name, Department
- Goal Status, Goal Type (Business/Development)
- SMART要素 (Specific, Measurable, Achievable, Relevant, Time-bound)
- Weight, Achievement %

### 評価CSV
- Employee ID, Name, Department
- Evaluation Status
- What Score (Self/Manager/Final)
- How Score (Self/Manager/Final)
- Comments

## 🔄 ワークフロー

```
1. 社員が目標を入力（下書き）
   ↓
2. 上長が承認/差戻し
   ↓
3. HR（管理者）が最終承認
   ↓
4. 目標確定
   ↓
5. 自己評価・上長評価・HR確認
   ↓
6. 1対1面談設定
   ↓
7. 完了
```

## 🐛 トラブルシューティング

### "Session state does not function" エラー
Streamlitコマンドで実行してください:
```bash
streamlit run EvaluationApps.py
```

### データベース接続エラー
PostgreSQL接続の場合:
```bash
export DATABASE_URL="postgresql+psycopg://user:password@host:5432/dbname"
streamlit run EvaluationApps.py
```

### パスワードリセット
管理者が従業員マスタから新規パスワードを発行：
1. 管理者でログイン
2. 「Employee Master」を選択
3. 従業員情報を編集し、新パスワードを入力
4. 社員に新パスワードを共有

## 📚 技術スタック

- **Frontend**: Streamlit
- **Backend**: Python / SQLAlchemy
- **Database**: PostgreSQL または SQLite
- **Authentication**: Password hashing (PBKDF2/Bcrypt)
- **Data Format**: CSV Export

## 📄 ライセンス

This application is developed for Stanley Black & Decker internal use.

## 👨‍💼 サポート

問題が発生した場合は、管理者にお問い合わせください。

---

**Version**: 1.0  
**Last Updated**: February 26, 2026  
**Stanley Black & Decker**
