import os
import importlib.util
import streamlit as st

# Prepare environment for test
os.environ["SMTP_SERVER"] = os.environ.get("SMTP_SERVER", "localhost")
os.environ["SMTP_PORT"] = os.environ.get("SMTP_PORT", "1025")
# Ensure Streamlit session state/secrets are available as dicts for the module
st.session_state = {}
st.secrets = {}

# Dynamically import EvaluationApps module without executing streamlit run
spec = importlib.util.spec_from_file_location("EvaluationApps", os.path.join(os.getcwd(), "EvaluationApps.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

print("Module loaded. Testing send_email paths...")

# 1) Debug path: no SMTP_USERNAME/PASSWORD -> should set email_debug in st.session_state and return False
os.environ.pop("SMTP_USERNAME", None)
os.environ.pop("SMTP_PASSWORD", None)
res_debug = module.send_email("devnull@example.com", "[Test Debug] 件名", "デバッグパスの本文。")
print("Debug path returned:", res_debug)
print("st.session_state keys:", list(st.session_state.keys()))
print("email_debug value:\n", st.session_state.get("email_debug"))

# 2) Auth path: set SMTP_USERNAME/PASSWORD (server likely doesn't support auth) -> exception expected
os.environ["SMTP_USERNAME"] = "user"
os.environ["SMTP_PASSWORD"] = "pass"
res_auth = module.send_email("devnull@example.com", "[Test Auth] 件名", "認証パスの本文。")
print("Auth path returned:", res_auth)
print("st.session_state email_error:\n", st.session_state.get("email_error"))
