import os
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path

import gradio as gr
import pandas as pd
import matplotlib.pyplot as plt

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
OUTPUT_DIR = APP_DIR / "outputs"
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "audit.db"
REQUIRED_COLUMNS = ["reference", "transaction_date", "amount"]

CSS = """
:root {--moza-red:#c8102e;--moza-dark:#8f0b20;--moza-light:#fff4f5;}
.gradio-container {max-width:1450px !important;}
#topbar {background:linear-gradient(90deg,var(--moza-dark),var(--moza-red));color:white;padding:22px 28px;border-radius:14px;margin-bottom:16px;}
#topbar h1,#topbar p {color:white !important;margin:0;}
.primary-btn {background:var(--moza-red) !important;color:white !important;}
.metric-card {border-left:5px solid var(--moza-red);background:var(--moza-light);padding:12px 16px;border-radius:10px;}
footer {display:none !important;}
"""

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT)""")
        conn.commit()

def write_log(username, action, details=""):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO audit_logs(created_at,username,action,details) VALUES (?,?,?,?)",
                     (datetime.now().isoformat(timespec="seconds"), username or "desconhecido", action, details))
        conn.commit()

def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def read_table(file_path):
    suffix = Path(file_path).suffix.lower()
    if suffix == ".csv":
        try:
            return pd.read_csv(file_path)
        except UnicodeDecodeError:
            return pd.read_csv(file_path, encoding="latin-1")
    if suffix in [".xlsx", ".xls"]:
        return pd.read_excel(file_path)
    raise ValueError("Formato não suportado. Use CSV, XLSX ou XLS.")

def normalize(df, source_name):
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"O ficheiro {source_name} não contém: {', '.join(missing)}")
    df["reference"] = df["reference"].astype(str).str.strip().str.upper()
    df["transaction_date"] = pd.to_datetime(df["transaction_date"], errors="coerce", dayfirst=True)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").round(2)
    if "status" not in df.columns:
        df["status"] = ""
    df["status"] = df["status"].fillna("").astype(str).str.strip().str.upper()
    df["row_number"] = range(2, len(df) + 2)
    invalid = df[df["reference"].eq("") | df["transaction_date"].isna() | df["amount"].isna()]
    if not invalid.empty:
        raise ValueError(f"O ficheiro {source_name} possui {len(invalid)} linha(s) inválida(s).")
    return df

def reconcile(source_file, target_file, request: gr.Request):
    username = getattr(request, "username", None) or "utilizador"
    if not source_file or not target_file:
        raise gr.Error("Carregue os dois ficheiros antes de reconciliar.")
    source_path = source_file if isinstance(source_file, str) else source_file.name
    target_path = target_file if isinstance(target_file, str) else target_file.name
    try:
        source = normalize(read_table(source_path), "ATM/EJ")
        target = normalize(read_table(target_path), "CORE")
    except Exception as exc:
        write_log(username, "FALHA_VALIDACAO", str(exc))
        raise gr.Error(str(exc))

    source["duplicate"] = source.duplicated(subset=["reference", "amount"], keep=False)
    target["duplicate"] = target.duplicated(subset=["reference", "amount"], keep=False)
    left = source.rename(columns={"transaction_date":"date_atm","amount":"amount_atm","status":"status_atm","row_number":"row_atm","duplicate":"duplicate_atm"})
    right = target.rename(columns={"transaction_date":"date_core","amount":"amount_core","status":"status_core","row_number":"row_core","duplicate":"duplicate_core"})
    result = left.merge(right[["reference","date_core","amount_core","status_core","row_core","duplicate_core"]], on="reference", how="outer", indicator=True)

    def classify(row):
        if row["_merge"] == "left_only": return "EXCEÇÃO: APENAS ATM/EJ"
        if row["_merge"] == "right_only": return "EXCEÇÃO: APENAS CORE"
        if bool(row.get("duplicate_atm", False)) or bool(row.get("duplicate_core", False)): return "EXCEÇÃO: DUPLICADO"
        if round(float(row["amount_atm"]),2) != round(float(row["amount_core"]),2): return "EXCEÇÃO: MONTANTE DIFERENTE"
        return "RECONCILIADO"

    result["reconciliation_status"] = result.apply(classify, axis=1)
    result["amount_difference"] = (result["amount_atm"].fillna(0) - result["amount_core"].fillna(0)).round(2)
    cols = ["reference","date_atm","date_core","amount_atm","amount_core","amount_difference","status_atm","status_core","reconciliation_status","row_atm","row_core"]
    result = result[cols].sort_values(["reconciliation_status","reference"]).reset_index(drop=True)

    total = len(result)
    reconciled = int((result["reconciliation_status"] == "RECONCILIADO").sum())
    exceptions = total - reconciled
    rate = (reconciled / total * 100) if total else 0
    exception_value = float(result.loc[result["reconciliation_status"] != "RECONCILIADO", "amount_difference"].abs().sum())

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    excel_path = OUTPUT_DIR / f"reconciliacao_atm_{timestamp}.xlsx"
    csv_path = OUTPUT_DIR / f"reconciliacao_atm_{timestamp}.csv"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        result.to_excel(writer, sheet_name="Resultado", index=False)
        result[result["reconciliation_status"] != "RECONCILIADO"].to_excel(writer, sheet_name="Excecoes", index=False)
        pd.DataFrame([{"total_registos":total,"reconciliados":reconciled,"excecoes":exceptions,"taxa_reconciliacao_percentagem":round(rate,2),"valor_absoluto_diferencas":round(exception_value,2),"hash_ficheiro_atm":file_hash(source_path),"hash_ficheiro_core":file_hash(target_path),"executado_por":username,"executado_em":datetime.now().isoformat(timespec="seconds")}]).to_excel(writer, sheet_name="Resumo", index=False)
    result.to_csv(csv_path, index=False)

    counts = result["reconciliation_status"].value_counts()
    fig, ax = plt.subplots(figsize=(8,4.5))
    counts.plot(kind="bar", ax=ax)
    ax.set_title("Resultado da Reconciliação ATM")
    ax.set_xlabel("")
    ax.set_ylabel("Quantidade")
    ax.tick_params(axis="x", rotation=25)
    fig.tight_layout()

    summary = f'''<div class="metric-card"><b>Total analisado:</b> {total:,}<br><b>Reconciliado:</b> {reconciled:,}<br><b>Exceções:</b> {exceptions:,}<br><b>Taxa de reconciliação:</b> {rate:.2f}%<br><b>Valor absoluto das diferenças:</b> {exception_value:,.2f} MZN</div>'''
    write_log(username, "RECONCILIACAO_ATM", f"Total={total}; Reconciliados={reconciled}; Excecoes={exceptions}")
    return summary, result, fig, str(excel_path), str(csv_path)

def load_logs(request: gr.Request):
    username = getattr(request, "username", None) or "utilizador"
    write_log(username, "CONSULTA_LOGS", "Consulta ao histórico")
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query("SELECT created_at,username,action,details FROM audit_logs ORDER BY id DESC LIMIT 200", conn)

init_db()
with gr.Blocks(css=CSS, title="Moza Reconcile") as demo:
    gr.HTML('<div id="topbar"><h1>MOZA RECONCILE</h1><p>Plataforma Integrada de Reconciliação e Gestão de Exceções</p></div>')
    with gr.Tab("Reconciliação ATM"):
        gr.Markdown("### Carregamento\nOs dois ficheiros devem conter `reference`, `transaction_date`, `amount` e, opcionalmente, `status`.")
        with gr.Row():
            source_file = gr.File(label="Ficheiro ATM / Electronic Journal", file_types=[".csv", ".xlsx", ".xls"], type="filepath")
            target_file = gr.File(label="Ficheiro Core Banking", file_types=[".csv", ".xlsx", ".xls"], type="filepath")
        run_btn = gr.Button("Executar reconciliação", elem_classes=["primary-btn"])
        summary = gr.HTML()
        results = gr.Dataframe(label="Resultado detalhado", interactive=False, wrap=True)
        chart = gr.Plot(label="Dashboard")
        with gr.Row():
            excel_download = gr.File(label="Relatório Excel")
            csv_download = gr.File(label="Resultado CSV")
        run_btn.click(reconcile, [source_file, target_file], [summary, results, chart, excel_download, csv_download])
    with gr.Tab("Auditoria e Logs"):
        refresh_logs = gr.Button("Atualizar logs")
        logs_table = gr.Dataframe(interactive=False, wrap=True)
        refresh_logs.click(load_logs, outputs=logs_table)
    with gr.Tab("Próximos módulos"):
        gr.Markdown("Gestão de exceções, Maker–Checker, Compensação, METIX, Interoperabilidade, POS, Float, Real Time, PDF e e-mail.")

if __name__ == "__main__":
    username = os.getenv("APP_USERNAME", "admin")
    password = os.getenv("APP_PASSWORD", "Moza@12345")
    port = int(os.getenv("PORT", "7860"))
    demo.launch(server_name="0.0.0.0", server_port=port, auth=[(username, password)], show_error=True)
