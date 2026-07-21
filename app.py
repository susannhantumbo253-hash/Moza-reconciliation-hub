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
.gradio-container {max-width:1500px !important;background:#fafafa;}
#topbar {background:linear-gradient(90deg,var(--moza-dark),var(--moza-red));color:white;padding:24px 30px;border-radius:16px;margin-bottom:18px;box-shadow:0 6px 20px rgba(143,11,32,.18);}
#topbar h1,#topbar p {color:white !important;margin:0;}
#topbar h1 {font-size:29px;font-weight:800;}
#topbar p {margin-top:6px;opacity:.95;}
.primary-btn {background:var(--moza-red) !important;color:white !important;border:none !important;font-weight:700 !important;}
.metric-card {border-left:5px solid var(--moza-red);background:var(--moza-light);padding:16px 18px;border-radius:12px;margin-bottom:8px;}
.dashboard-grid {display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:16px;}
.dashboard-card {background:white;border:1px solid #e7e7e7;padding:18px;border-radius:14px;min-height:110px;box-shadow:0 3px 12px rgba(0,0,0,.05);}
.dashboard-card h3 {color:#666 !important;font-size:14px;margin:0;font-weight:600;}
.dashboard-card h2 {color:var(--moza-red) !important;font-size:29px;margin:9px 0 0 0;}
.section-title {border-bottom:2px solid var(--moza-red);padding-bottom:7px;}
.module-note {background:white;border:1px solid #ececec;padding:14px 16px;border-radius:12px;}
footer {display:none !important;}
"""

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, username TEXT NOT NULL, action TEXT NOT NULL, details TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS reconciliation_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, username TEXT NOT NULL, process_type TEXT NOT NULL, total_records INTEGER NOT NULL, reconciled_records INTEGER NOT NULL, exception_records INTEGER NOT NULL, reconciliation_rate REAL NOT NULL, exception_value REAL NOT NULL, excel_report TEXT, csv_report TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS exceptions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, created_at TEXT NOT NULL, process_type TEXT NOT NULL, reference TEXT, exception_type TEXT NOT NULL, amount_source REAL, amount_target REAL, amount_difference REAL, status TEXT NOT NULL DEFAULT 'PENDENTE', assigned_to TEXT, resolution_comment TEXT, resolved_at TEXT, FOREIGN KEY(run_id) REFERENCES reconciliation_runs(id))""")
        conn.commit()

def write_log(username, action, details=""):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO audit_logs(created_at,username,action,details) VALUES (?,?,?,?)", (datetime.now().isoformat(timespec="seconds"), username or "desconhecido", action, details))
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

def safe_float(value):
    if pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def classify_transaction(row):
    if row["_merge"] == "left_only":
        return "EXCEÇÃO: APENAS ORIGEM"
    if row["_merge"] == "right_only":
        return "EXCEÇÃO: APENAS DESTINO"
    if bool(row.get("duplicate_source", False)) or bool(row.get("duplicate_target", False)):
        return "EXCEÇÃO: DUPLICADO"
    if round(float(row["amount_source"]), 2) != round(float(row["amount_target"]), 2):
        return "EXCEÇÃO: MONTANTE DIFERENTE"
    return "RECONCILIADO"

def save_reconciliation_run(username, process_type, total, reconciled, exceptions, rate, exception_value, excel_path, csv_path, result):
    created_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""INSERT INTO reconciliation_runs(created_at,username,process_type,total_records,reconciled_records,exception_records,reconciliation_rate,exception_value,excel_report,csv_report) VALUES (?,?,?,?,?,?,?,?,?,?)""", (created_at, username, process_type, total, reconciled, exceptions, rate, exception_value, str(excel_path), str(csv_path)))
        run_id = cursor.lastrowid
        for _, row in result[result["reconciliation_status"] != "RECONCILIADO"].iterrows():
            cursor.execute("""INSERT INTO exceptions(run_id,created_at,process_type,reference,exception_type,amount_source,amount_target,amount_difference,status) VALUES (?,?,?,?,?,?,?,?,?)""", (run_id, created_at, process_type, str(row.get("reference", "")), str(row.get("reconciliation_status", "")), safe_float(row.get("amount_source")), safe_float(row.get("amount_target")), safe_float(row.get("amount_difference")), "PENDENTE"))
        conn.commit()

def reconcile_process(source_file, target_file, process_type, source_name, target_name, request: gr.Request):
    username = getattr(request, "username", None) or "utilizador"
    if not source_file or not target_file:
        raise gr.Error("Carregue os dois ficheiros antes de executar a reconciliação.")
    source_path = source_file if isinstance(source_file, str) else source_file.name
    target_path = target_file if isinstance(target_file, str) else target_file.name
    try:
        source = normalize(read_table(source_path), source_name)
        target = normalize(read_table(target_path), target_name)
    except Exception as error:
        write_log(username, "FALHA_VALIDACAO", str(error))
        raise gr.Error(str(error))

    source["duplicate"] = source.duplicated(subset=["reference", "amount"], keep=False)
    target["duplicate"] = target.duplicated(subset=["reference", "amount"], keep=False)
    left = source.rename(columns={"transaction_date":"date_source","amount":"amount_source","status":"status_source","row_number":"row_source","duplicate":"duplicate_source"})
    right = target.rename(columns={"transaction_date":"date_target","amount":"amount_target","status":"status_target","row_number":"row_target","duplicate":"duplicate_target"})
    result = left.merge(right[["reference","date_target","amount_target","status_target","row_target","duplicate_target"]], on="reference", how="outer", indicator=True)
    result["reconciliation_status"] = result.apply(classify_transaction, axis=1)
    result["amount_difference"] = (result["amount_source"].fillna(0) - result["amount_target"].fillna(0)).round(2)
    cols = ["reference","date_source","date_target","amount_source","amount_target","amount_difference","status_source","status_target","reconciliation_status","row_source","row_target"]
    result = result[cols].sort_values(["reconciliation_status","reference"]).reset_index(drop=True)

    total = len(result)
    reconciled = int((result["reconciliation_status"] == "RECONCILIADO").sum())
    exceptions = total - reconciled
    rate = reconciled / total * 100 if total else 0
    exception_value = float(result.loc[result["reconciliation_status"] != "RECONCILIADO", "amount_difference"].abs().sum())

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    process_file_name = process_type.lower().replace(" ", "_")
    excel_path = OUTPUT_DIR / f"reconciliacao_{process_file_name}_{timestamp}.xlsx"
    csv_path = OUTPUT_DIR / f"reconciliacao_{process_file_name}_{timestamp}.csv"

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        result.to_excel(writer, sheet_name="Resultado", index=False)
        result[result["reconciliation_status"] != "RECONCILIADO"].to_excel(writer, sheet_name="Excecoes", index=False)
        pd.DataFrame([{"processo":process_type,"total_registos":total,"reconciliados":reconciled,"excecoes":exceptions,"taxa_reconciliacao_percentagem":round(rate,2),"valor_absoluto_diferencas":round(exception_value,2),"hash_ficheiro_origem":file_hash(source_path),"hash_ficheiro_destino":file_hash(target_path),"executado_por":username,"executado_em":datetime.now().isoformat(timespec="seconds")}]).to_excel(writer, sheet_name="Resumo", index=False)
    result.to_csv(csv_path, index=False)

    save_reconciliation_run(username, process_type, total, reconciled, exceptions, rate, exception_value, excel_path, csv_path, result)

    counts = result["reconciliation_status"].value_counts()
    figure, axis = plt.subplots(figsize=(8, 4.5))
    counts.plot(kind="bar", ax=axis)
    axis.set_title(f"Resultado da Reconciliação {process_type}")
    axis.set_xlabel("")
    axis.set_ylabel("Quantidade")
    axis.tick_params(axis="x", rotation=25)
    figure.tight_layout()

    summary_html = f'''<div class="metric-card"><b>Processo:</b> {process_type}<br><b>Total analisado:</b> {total:,}<br><b>Reconciliado:</b> {reconciled:,}<br><b>Exceções:</b> {exceptions:,}<br><b>Taxa de reconciliação:</b> {rate:.2f}%<br><b>Valor absoluto das diferenças:</b> {exception_value:,.2f} MZN</div>'''
    write_log(username, f"RECONCILIACAO_{process_type}", f"Total={total}; Reconciliados={reconciled}; Excecoes={exceptions}")
    return summary_html, result, figure, str(excel_path), str(csv_path)

def reconcile_atm(source_file, target_file, request: gr.Request):
    return reconcile_process(source_file, target_file, "ATM", "ATM/EJ", "CORE ATM", request)

def reconcile_pos(source_file, target_file, request: gr.Request):
    return reconcile_process(source_file, target_file, "POS", "POS", "CORE POS", request)

def reconcile_metix(source_file, target_file, request: gr.Request):
    return reconcile_process(source_file, target_file, "METIX", "METIX", "CORE METIX", request)

def load_dashboard():
    with sqlite3.connect(DB_PATH) as conn:
        totals = pd.read_sql_query("""SELECT COUNT(*) AS total_runs, COALESCE(SUM(total_records),0) AS total_records, COALESCE(SUM(reconciled_records),0) AS reconciled_records, COALESCE(SUM(exception_records),0) AS exception_records, COALESCE(SUM(exception_value),0) AS exception_value FROM reconciliation_runs""", conn).iloc[0]
        pending = pd.read_sql_query("SELECT COUNT(*) AS total FROM exceptions WHERE status='PENDENTE'", conn).iloc[0]["total"]
        history = pd.read_sql_query("""SELECT id AS execucao, created_at AS data, username AS utilizador, process_type AS processo, total_records AS total, reconciled_records AS reconciliados, exception_records AS excecoes, ROUND(reconciliation_rate,2) AS taxa_percentagem FROM reconciliation_runs ORDER BY id DESC LIMIT 20""", conn)
    total_records = int(totals["total_records"])
    reconciled_records = int(totals["reconciled_records"])
    general_rate = reconciled_records / total_records * 100 if total_records else 0
    html = f'''<div class="dashboard-grid"><div class="dashboard-card"><h3>Execuções</h3><h2>{int(totals["total_runs"]):,}</h2></div><div class="dashboard-card"><h3>Transações analisadas</h3><h2>{total_records:,}</h2></div><div class="dashboard-card"><h3>Reconciliadas</h3><h2>{reconciled_records:,}</h2></div><div class="dashboard-card"><h3>Exceções encontradas</h3><h2>{int(totals["exception_records"]):,}</h2></div><div class="dashboard-card"><h3>Exceções pendentes</h3><h2>{int(pending):,}</h2></div><div class="dashboard-card"><h3>Taxa geral</h3><h2>{general_rate:.2f}%</h2></div><div class="dashboard-card"><h3>Valor das diferenças</h3><h2>{float(totals["exception_value"]):,.2f} MZN</h2></div></div>'''
    return html, history

def load_exceptions():
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query("""SELECT id, created_at AS data, process_type AS processo, reference AS referencia, exception_type AS tipo_excecao, amount_source AS valor_origem, amount_target AS valor_destino, amount_difference AS diferenca, status, assigned_to AS responsavel, resolution_comment AS comentario FROM exceptions ORDER BY id DESC LIMIT 500""", conn)

def resolve_exception(exception_id, new_status, assigned_to, comment, request: gr.Request):
    username = getattr(request, "username", None) or "utilizador"
    if exception_id is None:
        raise gr.Error("Informe o ID da exceção.")
    exception_id = int(exception_id)
    resolved_at = datetime.now().isoformat(timespec="seconds") if new_status in ["RESOLVIDA", "REJEITADA"] else None
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE exceptions SET status=?, assigned_to=?, resolution_comment=?, resolved_at=? WHERE id=?", (new_status, assigned_to or username, comment or "", resolved_at, exception_id))
        if cursor.rowcount == 0:
            raise gr.Error("Não foi encontrada uma exceção com esse ID.")
        conn.commit()
    write_log(username, "ATUALIZACAO_EXCECAO", f"Excecao={exception_id}; Estado={new_status}")
    return f"Exceção {exception_id} atualizada para {new_status}.", load_exceptions()

def load_reports():
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query("""SELECT id AS execucao, created_at AS data, username AS utilizador, process_type AS processo, total_records AS total, reconciled_records AS reconciliados, exception_records AS excecoes, ROUND(reconciliation_rate,2) AS taxa_percentagem, ROUND(exception_value,2) AS valor_diferencas FROM reconciliation_runs ORDER BY id DESC LIMIT 200""", conn)

def load_logs(request: gr.Request):
    username = getattr(request, "username", None) or "utilizador"
    write_log(username, "CONSULTA_LOGS", "Consulta ao histórico de auditoria")
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query("""SELECT created_at AS data, username AS utilizador, action AS acao, details AS detalhes FROM audit_logs ORDER BY id DESC LIMIT 300""", conn)

init_db()

with gr.Blocks(css=CSS, title="Moza Reconciliation Hub") as demo:
    gr.HTML('<div id="topbar"><h1>MOZA RECONCILIATION HUB</h1><p>Plataforma Integrada de Reconciliação, Gestão de Exceções e Auditoria</p></div>')

    with gr.Tab("Dashboard"):
        gr.Markdown("## Visão Geral", elem_classes=["section-title"])
        refresh_dashboard = gr.Button("Atualizar Dashboard", elem_classes=["primary-btn"])
        dashboard_cards = gr.HTML()
        gr.Markdown("### Histórico recente de reconciliações")
        dashboard_history = gr.Dataframe(interactive=False, wrap=True)
        refresh_dashboard.click(load_dashboard, outputs=[dashboard_cards, dashboard_history])

    with gr.Tab("Reconciliação ATM"):
        gr.Markdown("### Carregamento de ficheiros ATM\nOs ficheiros devem conter `reference`, `transaction_date`, `amount`. A coluna `status` é opcional.")
        with gr.Row():
            atm_source_file = gr.File(label="Ficheiro ATM / Electronic Journal", file_types=[".csv", ".xlsx", ".xls"], type="filepath")
            atm_target_file = gr.File(label="Ficheiro Core Banking ATM", file_types=[".csv", ".xlsx", ".xls"], type="filepath")
        atm_run_button = gr.Button("Executar reconciliação ATM", elem_classes=["primary-btn"])
        atm_summary = gr.HTML()
        atm_results = gr.Dataframe(label="Resultado detalhado ATM", interactive=False, wrap=True)
        atm_chart = gr.Plot(label="Dashboard ATM")
        with gr.Row():
            atm_excel_download = gr.File(label="Relatório ATM em Excel")
            atm_csv_download = gr.File(label="Resultado ATM em CSV")
        atm_run_button.click(reconcile_atm, [atm_source_file, atm_target_file], [atm_summary, atm_results, atm_chart, atm_excel_download, atm_csv_download])

    with gr.Tab("Reconciliação POS"):
        gr.Markdown("### Carregamento de ficheiros POS\nOs ficheiros devem conter `reference`, `transaction_date`, `amount`. A coluna `status` é opcional.")
        with gr.Row():
            pos_source_file = gr.File(label="Ficheiro de transações POS", file_types=[".csv", ".xlsx", ".xls"], type="filepath")
            pos_target_file = gr.File(label="Ficheiro Core Banking POS", file_types=[".csv", ".xlsx", ".xls"], type="filepath")
        pos_run_button = gr.Button("Executar reconciliação POS", elem_classes=["primary-btn"])
        pos_summary = gr.HTML()
        pos_results = gr.Dataframe(label="Resultado detalhado POS", interactive=False, wrap=True)
        pos_chart = gr.Plot(label="Dashboard POS")
        with gr.Row():
            pos_excel_download = gr.File(label="Relatório POS em Excel")
            pos_csv_download = gr.File(label="Resultado POS em CSV")
        pos_run_button.click(reconcile_pos, [pos_source_file, pos_target_file], [pos_summary, pos_results, pos_chart, pos_excel_download, pos_csv_download])

    with gr.Tab("Reconciliação METIX"):
        gr.Markdown("### Carregamento de ficheiros METIX\nOs ficheiros devem conter `reference`, `transaction_date`, `amount`. As colunas `transaction_id`, `account_number`, `transaction_type`, `currency` e `status` podem ser mantidas para análise operacional.")
        with gr.Row():
            metix_source_file = gr.File(label="Ficheiro de transações METIX", file_types=[".csv", ".xlsx", ".xls"], type="filepath")
            metix_target_file = gr.File(label="Ficheiro Core Banking METIX", file_types=[".csv", ".xlsx", ".xls"], type="filepath")
        metix_run_button = gr.Button("Executar reconciliação METIX", elem_classes=["primary-btn"])
        metix_summary = gr.HTML()
        metix_results = gr.Dataframe(label="Resultado detalhado METIX", interactive=False, wrap=True)
        metix_chart = gr.Plot(label="Dashboard METIX")
        with gr.Row():
            metix_excel_download = gr.File(label="Relatório METIX em Excel")
            metix_csv_download = gr.File(label="Resultado METIX em CSV")
        metix_run_button.click(reconcile_metix, [metix_source_file, metix_target_file], [metix_summary, metix_results, metix_chart, metix_excel_download, metix_csv_download])

    with gr.Tab("Gestão de Exceções"):
        gr.Markdown("## Exceções", elem_classes=["section-title"])
        refresh_exceptions = gr.Button("Atualizar lista de exceções")
        exceptions_table = gr.Dataframe(interactive=False, wrap=True)
        refresh_exceptions.click(load_exceptions, outputs=exceptions_table)
        gr.Markdown("### Atualizar uma exceção")
        with gr.Row():
            exception_id = gr.Number(label="ID da exceção", precision=0)
            exception_status = gr.Dropdown(choices=["PENDENTE","EM ANÁLISE","RESOLVIDA","REJEITADA"], value="EM ANÁLISE", label="Novo estado")
            exception_responsible = gr.Textbox(label="Responsável")
        exception_comment = gr.Textbox(label="Comentário da resolução", lines=3)
        update_exception_button = gr.Button("Guardar atualização", elem_classes=["primary-btn"])
        exception_message = gr.Textbox(label="Resultado", interactive=False)
        update_exception_button.click(resolve_exception, [exception_id, exception_status, exception_responsible, exception_comment], [exception_message, exceptions_table])

    with gr.Tab("Relatórios"):
        gr.Markdown("## Histórico de Relatórios", elem_classes=["section-title"])
        refresh_reports = gr.Button("Atualizar relatórios")
        reports_table = gr.Dataframe(interactive=False, wrap=True)
        refresh_reports.click(load_reports, outputs=reports_table)

    with gr.Tab("Auditoria e Logs"):
        gr.Markdown("## Registo de Atividades", elem_classes=["section-title"])
        refresh_logs = gr.Button("Atualizar logs")
        logs_table = gr.Dataframe(interactive=False, wrap=True)
        refresh_logs.click(load_logs, outputs=logs_table)

    with gr.Tab("Administração"):
        gr.Markdown('<div class="module-note"><h3>Administração</h3><p>Área preparada para gestão de utilizadores, perfis e permissões.</p><p>Perfis previstos: Administrador, Supervisor, Operador, Auditor e Gestor.</p></div>')

    with gr.Tab("Próximos Módulos"):
        gr.Markdown('<div class="module-note"><h3>Roadmap funcional</h3><ul><li>METIX — implementado</li><li>Compensação</li><li>Interoperabilidade</li><li>Conta Float</li><li>Conta Real Time</li><li>Maker–Checker</li><li>PDF e envio automático por e-mail</li><li>Integração com Power Automate e Power BI</li></ul></div>')

if __name__ == "__main__":
    username = os.getenv("APP_USERNAME", "admin")
    password = os.getenv("APP_PASSWORD", "Moza@12345")
    port = int(os.getenv("PORT", "7860"))
    demo.launch(server_name="0.0.0.0", server_port=port, auth=[(username, password)], show_error=True)
