import os
import sqlite3
from datetime import datetime
from pathlib import Path

import gradio as gr
import matplotlib.pyplot as plt
import pandas as pd


# =========================================================
# CONFIGURAÇÕES
# =========================================================

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
OUTPUT_DIR = APP_DIR / "outputs"
DB_PATH = DATA_DIR / "reconciliation.db"

DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "Moza@12345")
PORT = int(os.getenv("PORT", "7860"))

REQUIRED_COLUMNS = ["reference", "transaction_date", "amount"]

PROCESS_CONFIG = {
    "ATM": ("ATM / Electronic Journal", "Core Banking ATM"),
    "POS": ("POS", "Core Banking POS"),
    "METIX": ("METIX", "Core Banking METIX"),
    "COMPENSAÇÃO": ("Compensação", "Core Banking Compensação"),
}


# =========================================================
# BASE DE DADOS
# =========================================================

def get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reconciliation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                username TEXT NOT NULL,
                process_type TEXT NOT NULL,
                total_records INTEGER NOT NULL,
                reconciled_records INTEGER NOT NULL,
                exception_records INTEGER NOT NULL,
                reconciliation_rate REAL NOT NULL,
                exception_value REAL NOT NULL,
                excel_report TEXT,
                csv_report TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS exceptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                process_type TEXT NOT NULL,
                reference TEXT,
                exception_type TEXT NOT NULL,
                amount_source REAL,
                amount_target REAL,
                amount_difference REAL,
                status TEXT NOT NULL DEFAULT 'PENDENTE',
                assigned_to TEXT,
                resolution_comment TEXT,
                resolved_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT
            )
        """)

        conn.commit()


def write_log(username, action, details=""):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs(created_at, username, action, details)
            VALUES (?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                username or "utilizador",
                action,
                details,
            ),
        )
        conn.commit()


def safe_float(value):
    if pd.isna(value):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# =========================================================
# LEITURA E VALIDAÇÃO DOS FICHEIROS
# =========================================================

def read_table(file_path):
    extension = Path(file_path).suffix.lower()

    if extension == ".csv":
        try:
            return pd.read_csv(file_path)
        except UnicodeDecodeError:
            return pd.read_csv(file_path, encoding="latin-1")

    if extension in [".xlsx", ".xls"]:
        return pd.read_excel(file_path)

    raise ValueError("Formato não suportado. Utilize CSV, XLSX ou XLS.")


def normalize_table(dataframe, file_name):
    dataframe = dataframe.copy()

    dataframe.columns = [
        str(column).strip().lower()
        for column in dataframe.columns
    ]

    missing = [
        column
        for column in REQUIRED_COLUMNS
        if column not in dataframe.columns
    ]

    if missing:
        raise ValueError(
            f"O ficheiro {file_name} não contém as colunas: "
            f"{', '.join(missing)}"
        )

    dataframe["reference"] = (
        dataframe["reference"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    dataframe["transaction_date"] = pd.to_datetime(
        dataframe["transaction_date"],
        errors="coerce",
        dayfirst=True,
    )

    dataframe["amount"] = pd.to_numeric(
        dataframe["amount"],
        errors="coerce",
    ).round(2)

    if "status" not in dataframe.columns:
        dataframe["status"] = ""

    dataframe["status"] = (
        dataframe["status"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
    )

    dataframe["row_number"] = range(2, len(dataframe) + 2)

    invalid = dataframe[
        dataframe["reference"].eq("")
        | dataframe["transaction_date"].isna()
        | dataframe["amount"].isna()
    ]

    if not invalid.empty:
        raise ValueError(
            f"O ficheiro {file_name} possui "
            f"{len(invalid)} linha(s) inválida(s)."
        )

    return dataframe


# =========================================================
# MOTOR DE RECONCILIAÇÃO
# =========================================================

def classify_row(row):
    if row["_merge"] == "left_only":
        return "EXCEÇÃO: APENAS ORIGEM"

    if row["_merge"] == "right_only":
        return "EXCEÇÃO: APENAS DESTINO"

    if bool(row.get("duplicate_source", False)) or bool(
        row.get("duplicate_target", False)
    ):
        return "EXCEÇÃO: DUPLICADO"

    if round(float(row["amount_source"]), 2) != round(
        float(row["amount_target"]), 2
    ):
        return "EXCEÇÃO: MONTANTE DIFERENTE"

    return "RECONCILIADO"


def save_run(
    username,
    process_type,
    total,
    reconciled,
    exceptions_count,
    rate,
    exception_value,
    excel_path,
    csv_path,
    result,
):
    created_at = datetime.now().isoformat(timespec="seconds")

    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO reconciliation_runs(
                created_at, username, process_type, total_records,
                reconciled_records, exception_records,
                reconciliation_rate, exception_value,
                excel_report, csv_report
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                username,
                process_type,
                total,
                reconciled,
                exceptions_count,
                rate,
                exception_value,
                str(excel_path),
                str(csv_path),
            ),
        )

        run_id = cursor.lastrowid

        exception_rows = result[
            result["reconciliation_status"] != "RECONCILIADO"
        ]

        for _, row in exception_rows.iterrows():
            cursor.execute(
                """
                INSERT INTO exceptions(
                    run_id, created_at, process_type, reference,
                    exception_type, amount_source, amount_target,
                    amount_difference, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    created_at,
                    process_type,
                    str(row.get("reference", "")),
                    str(row.get("reconciliation_status", "")),
                    safe_float(row.get("amount_source")),
                    safe_float(row.get("amount_target")),
                    safe_float(row.get("amount_difference")),
                    "PENDENTE",
                ),
            )

        conn.commit()


def reconcile_process(
    source_file,
    target_file,
    process_type,
    request: gr.Request,
):
    username = getattr(request, "username", None) or "utilizador"

    if not source_file or not target_file:
        raise gr.Error("Carregue os dois ficheiros.")

    source_path = (
        source_file
        if isinstance(source_file, str)
        else source_file.name
    )

    target_path = (
        target_file
        if isinstance(target_file, str)
        else target_file.name
    )

    source_name, target_name = PROCESS_CONFIG[process_type]

    try:
        source = normalize_table(
            read_table(source_path),
            source_name,
        )

        target = normalize_table(
            read_table(target_path),
            target_name,
        )
    except Exception as error:
        write_log(username, "FALHA_VALIDACAO", str(error))
        raise gr.Error(str(error))

    source["duplicate"] = source.duplicated(
        subset=["reference", "amount"],
        keep=False,
    )

    target["duplicate"] = target.duplicated(
        subset=["reference", "amount"],
        keep=False,
    )

    source = source.rename(
        columns={
            "transaction_date": "date_source",
            "amount": "amount_source",
            "status": "status_source",
            "row_number": "row_source",
            "duplicate": "duplicate_source",
        }
    )

    target = target.rename(
        columns={
            "transaction_date": "date_target",
            "amount": "amount_target",
            "status": "status_target",
            "row_number": "row_target",
            "duplicate": "duplicate_target",
        }
    )

    result = source.merge(
        target[
            [
                "reference",
                "date_target",
                "amount_target",
                "status_target",
                "row_target",
                "duplicate_target",
            ]
        ],
        on="reference",
        how="outer",
        indicator=True,
    )

    result["reconciliation_status"] = result.apply(
        classify_row,
        axis=1,
    )

    result["amount_difference"] = (
        result["amount_source"].fillna(0)
        - result["amount_target"].fillna(0)
    ).round(2)

    result = result[
        [
            "reference",
            "date_source",
            "date_target",
            "amount_source",
            "amount_target",
            "amount_difference",
            "status_source",
            "status_target",
            "reconciliation_status",
            "row_source",
            "row_target",
        ]
    ].sort_values(
        ["reconciliation_status", "reference"]
    ).reset_index(drop=True)

    total = len(result)

    reconciled = int(
        (result["reconciliation_status"] == "RECONCILIADO").sum()
    )

    exceptions_count = total - reconciled

    rate = reconciled / total * 100 if total else 0

    exception_value = float(
        result.loc[
            result["reconciliation_status"] != "RECONCILIADO",
            "amount_difference",
        ].abs().sum()
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    safe_process_name = (
        process_type.lower()
        .replace(" ", "_")
        .replace("ç", "c")
        .replace("ã", "a")
        .replace("é", "e")
    )

    excel_path = OUTPUT_DIR / (
        f"reconciliacao_{safe_process_name}_{timestamp}.xlsx"
    )

    csv_path = OUTPUT_DIR / (
        f"reconciliacao_{safe_process_name}_{timestamp}.csv"
    )

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        result.to_excel(
            writer,
            sheet_name="Resultado",
            index=False,
        )

        result[
            result["reconciliation_status"] != "RECONCILIADO"
        ].to_excel(
            writer,
            sheet_name="Excecoes",
            index=False,
        )

        summary = pd.DataFrame(
            [
                {
                    "processo": process_type,
                    "total_registos": total,
                    "reconciliados": reconciled,
                    "excecoes": exceptions_count,
                    "taxa_reconciliacao": round(rate, 2),
                    "valor_diferencas": round(exception_value, 2),
                    "executado_por": username,
                    "executado_em": datetime.now().isoformat(
                        timespec="seconds"
                    ),
                }
            ]
        )

        summary.to_excel(
            writer,
            sheet_name="Resumo",
            index=False,
        )

    result.to_csv(csv_path, index=False)

    save_run(
        username,
        process_type,
        total,
        reconciled,
        exceptions_count,
        rate,
        exception_value,
        excel_path,
        csv_path,
        result,
    )

    counts = result["reconciliation_status"].value_counts()

    figure, axis = plt.subplots(figsize=(8, 4.5))
    counts.plot(kind="bar", ax=axis)
    axis.set_title(f"Reconciliação {process_type}")
    axis.set_xlabel("")
    axis.set_ylabel("Quantidade")
    axis.tick_params(axis="x", rotation=25)
    figure.tight_layout()

    summary_html = f"""
    <div class="metric-card">
        <b>Processo:</b> {process_type}<br>
        <b>Total analisado:</b> {total}<br>
        <b>Reconciliado:</b> {reconciled}<br>
        <b>Exceções:</b> {exceptions_count}<br>
        <b>Taxa de reconciliação:</b> {rate:.2f}%<br>
        <b>Valor absoluto das diferenças:</b>
        {exception_value:,.2f} MZN
    </div>
    """

    write_log(
        username,
        f"RECONCILIACAO_{process_type}",
        (
            f"Total={total}; "
            f"Reconciliados={reconciled}; "
            f"Excecoes={exceptions_count}"
        ),
    )

    return (
        summary_html,
        result,
        figure,
        str(excel_path),
        str(csv_path),
    )


def reconcile_atm(source, target, request: gr.Request):
    return reconcile_process(
        source,
        target,
        "ATM",
        request,
    )


def reconcile_pos(source, target, request: gr.Request):
    return reconcile_process(
        source,
        target,
        "POS",
        request,
    )


def reconcile_metix(source, target, request: gr.Request):
    return reconcile_process(
        source,
        target,
        "METIX",
        request,
    )


def reconcile_compensation(
    source,
    target,
    request: gr.Request,
):
    return reconcile_process(
        source,
        target,
        "COMPENSAÇÃO",
        request,
    )


# =========================================================
# DASHBOARD E GESTÃO
# =========================================================

def load_dashboard():
    with get_connection() as conn:
        totals = pd.read_sql_query(
            """
            SELECT
                COUNT(*) AS total_runs,
                COALESCE(SUM(total_records), 0) AS total_records,
                COALESCE(SUM(reconciled_records), 0) AS reconciled_records,
                COALESCE(SUM(exception_records), 0) AS exception_records,
                COALESCE(SUM(exception_value), 0) AS exception_value
            FROM reconciliation_runs
            """,
            conn,
        ).iloc[0]

        pending = pd.read_sql_query(
            """
            SELECT COUNT(*) AS total
            FROM exceptions
            WHERE status = 'PENDENTE'
            """,
            conn,
        ).iloc[0]["total"]

        history = pd.read_sql_query(
            """
            SELECT
                id AS execucao,
                created_at AS data,
                username AS utilizador,
                process_type AS processo,
                total_records AS total,
                reconciled_records AS reconciliados,
                exception_records AS excecoes,
                ROUND(reconciliation_rate, 2) AS taxa_percentagem
            FROM reconciliation_runs
            ORDER BY id DESC
            LIMIT 20
            """,
            conn,
        )

        process_summary = pd.read_sql_query(
            """
            SELECT
                process_type AS processo,
                COUNT(*) AS execucoes,
                SUM(total_records) AS total_transacoes,
                SUM(reconciled_records) AS reconciliadas,
                SUM(exception_records) AS excecoes,
                ROUND(AVG(reconciliation_rate), 2) AS taxa_media
            FROM reconciliation_runs
            GROUP BY process_type
            ORDER BY process_type
            """,
            conn,
        )

    total_records = int(totals["total_records"])
    reconciled_records = int(totals["reconciled_records"])

    general_rate = (
        reconciled_records / total_records * 100
        if total_records
        else 0
    )

    cards = f"""
    <div class="dashboard-grid">
        <div class="dashboard-card">
            <h3>Execuções</h3>
            <h2>{int(totals["total_runs"])}</h2>
        </div>

        <div class="dashboard-card">
            <h3>Transações analisadas</h3>
            <h2>{total_records}</h2>
        </div>

        <div class="dashboard-card">
            <h3>Reconciliadas</h3>
            <h2>{reconciled_records}</h2>
        </div>

        <div class="dashboard-card">
            <h3>Exceções encontradas</h3>
            <h2>{int(totals["exception_records"])}</h2>
        </div>

        <div class="dashboard-card">
            <h3>Exceções pendentes</h3>
            <h2>{int(pending)}</h2>
        </div>

        <div class="dashboard-card">
            <h3>Taxa geral</h3>
            <h2>{general_rate:.2f}%</h2>
        </div>

        <div class="dashboard-card">
            <h3>Valor das diferenças</h3>
            <h2>{float(totals["exception_value"]):,.2f} MZN</h2>
        </div>
    </div>
    """

    figure, axis = plt.subplots(figsize=(8, 4.5))

    if process_summary.empty:
        axis.text(
            0.5,
            0.5,
            "Ainda não existem reconciliações.",
            ha="center",
            va="center",
        )

        axis.set_axis_off()

    else:
        process_summary.plot(
            x="processo",
            y="excecoes",
            kind="bar",
            ax=axis,
            legend=False,
        )

        axis.set_title("Exceções por processo")
        axis.set_xlabel("")
        axis.set_ylabel("Quantidade")
        axis.tick_params(axis="x", rotation=20)

    figure.tight_layout()

    return (
        cards,
        history,
        process_summary,
        figure,
    )


def load_exceptions():
    with get_connection() as conn:
        return pd.read_sql_query(
            """
            SELECT
                id,
                created_at AS data,
                process_type AS processo,
                reference AS referencia,
                exception_type AS tipo_excecao,
                amount_source AS valor_origem,
                amount_target AS valor_destino,
                amount_difference AS diferenca,
                status,
                assigned_to AS responsavel,
                resolution_comment AS comentario
            FROM exceptions
            ORDER BY id DESC
            LIMIT 500
            """,
            conn,
        )


def update_exception(
    exception_id,
    new_status,
    assigned_to,
    comment,
    request: gr.Request,
):
    username = getattr(request, "username", None) or "utilizador"

    if exception_id is None:
        raise gr.Error("Informe o ID da exceção.")

    exception_id = int(exception_id)

    resolved_at = (
        datetime.now().isoformat(timespec="seconds")
        if new_status in ["RESOLVIDA", "REJEITADA"]
        else None
    )

    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE exceptions
            SET
                status = ?,
                assigned_to = ?,
                resolution_comment = ?,
                resolved_at = ?
            WHERE id = ?
            """,
            (
                new_status,
                assigned_to or username,
                comment or "",
                resolved_at,
                exception_id,
            ),
        )

        if cursor.rowcount == 0:
            raise gr.Error("Exceção não encontrada.")

        conn.commit()

    write_log(
        username,
        "ATUALIZACAO_EXCECAO",
        f"ID={exception_id}; Estado={new_status}",
    )

    return (
        f"Exceção {exception_id} atualizada com sucesso.",
        load_exceptions(),
    )


def load_reports():
    with get_connection() as conn:
        return pd.read_sql_query(
            """
            SELECT
                id AS execucao,
                created_at AS data,
                username AS utilizador,
                process_type AS processo,
                total_records AS total,
                reconciled_records AS reconciliados,
                exception_records AS excecoes,
                ROUND(reconciliation_rate, 2) AS taxa_percentagem,
                ROUND(exception_value, 2) AS valor_diferencas
            FROM reconciliation_runs
            ORDER BY id DESC
            LIMIT 200
            """,
            conn,
        )


def load_logs(request: gr.Request):
    username = getattr(request, "username", None) or "utilizador"

    write_log(
        username,
        "CONSULTA_LOGS",
        "Consulta do histórico de auditoria",
    )

    with get_connection() as conn:
        return pd.read_sql_query(
            """
            SELECT
                created_at AS data,
                username AS utilizador,
                action AS acao,
                details AS detalhes
            FROM audit_logs
            ORDER BY id DESC
            LIMIT 300
            """,
            conn,
        )


# =========================================================
# COMPONENTE DE RECONCILIAÇÃO
# =========================================================

def build_reconciliation_tab(
    process_name,
    source_label,
    target_label,
    reconcile_function,
):
    gr.Markdown(
        f"""
        ### Reconciliação {process_name}

        Os dois ficheiros devem conter estas colunas:

        `reference`, `transaction_date`, `amount`

        A coluna `status` é opcional.
        """
    )

    with gr.Row():
        source_file = gr.File(
            label=source_label,
            file_types=[".csv", ".xlsx", ".xls"],
            type="filepath",
        )

        target_file = gr.File(
            label=target_label,
            file_types=[".csv", ".xlsx", ".xls"],
            type="filepath",
        )

    run_button = gr.Button(
        f"Executar reconciliação {process_name}",
        elem_classes=["primary-btn"],
    )

    result_summary = gr.HTML()

    result_table = gr.Dataframe(
        label="Resultado detalhado",
        interactive=False,
        wrap=True,
    )

    result_chart = gr.Plot(
        label="Gráfico do resultado"
    )

    with gr.Row():
        excel_download = gr.File(
            label="Descarregar relatório Excel"
        )

        csv_download = gr.File(
            label="Descarregar relatório CSV"
        )

    run_button.click(
        reconcile_function,
        inputs=[
            source_file,
            target_file,
        ],
        outputs=[
            result_summary,
            result_table,
            result_chart,
            excel_download,
            csv_download,
        ],
    )


# =========================================================
# ESTILO
# =========================================================

CSS = """
:root {
    --main-red: #c8102e;
    --dark-red: #8f0b20;
    --light-red: #fff4f5;
}

.gradio-container {
    max-width: 1500px !important;
    background: #fafafa;
}

#topbar {
    background: linear-gradient(
        90deg,
        var(--dark-red),
        var(--main-red)
    );

    color: white;
    padding: 24px 30px;
    border-radius: 16px;
    margin-bottom: 18px;
}

#topbar h1,
#topbar p {
    color: white !important;
    margin: 0;
}

#topbar h1 {
    font-size: 29px;
    font-weight: 800;
}

#topbar p {
    margin-top: 6px;
}

.primary-btn {
    background: var(--main-red) !important;
    color: white !important;
    border: none !important;
    font-weight: 700 !important;
}

.metric-card {
    border-left: 5px solid var(--main-red);
    background: var(--light-red);
    padding: 16px 18px;
    border-radius: 12px;
    margin-bottom: 8px;
}

.dashboard-grid {
    display: grid;

    grid-template-columns: repeat(
        auto-fit,
        minmax(190px, 1fr)
    );

    gap: 14px;
    margin-bottom: 16px;
}

.dashboard-card {
    background: white;
    border: 1px solid #e7e7e7;
    padding: 18px;
    border-radius: 14px;
    min-height: 110px;
}

.dashboard-card h3 {
    color: #666 !important;
    font-size: 14px;
    margin: 0;
}

.dashboard-card h2 {
    color: var(--main-red) !important;
    font-size: 29px;
    margin: 9px 0 0 0;
}

.section-title {
    border-bottom: 2px solid var(--main-red);
    padding-bottom: 7px;
}

footer {
    display: none !important;
}
"""


# =========================================================
# INTERFACE
# =========================================================

init_db()

with gr.Blocks(
    css=CSS,
    title="Moza Reconciliation Hub",
) as demo:

    gr.HTML(
        """
        <div id="topbar">
            <h1>MOZA RECONCILIATION HUB</h1>

            <p>
                Plataforma Integrada de Reconciliação,
                Gestão de Exceções e Auditoria
            </p>
        </div>
        """
    )

    with gr.Tab("Dashboard"):
        gr.Markdown(
            "## Visão Geral",
            elem_classes=["section-title"],
        )

        refresh_dashboard = gr.Button(
            "Atualizar Dashboard",
            elem_classes=["primary-btn"],
        )

        dashboard_cards = gr.HTML()

        dashboard_history = gr.Dataframe(
            label="Histórico recente",
            interactive=False,
            wrap=True,
        )

        dashboard_process_summary = gr.Dataframe(
            label="Resumo por processo",
            interactive=False,
            wrap=True,
        )

        dashboard_chart = gr.Plot(
            label="Exceções por processo"
        )

        refresh_dashboard.click(
            load_dashboard,
            outputs=[
                dashboard_cards,
                dashboard_history,
                dashboard_process_summary,
                dashboard_chart,
            ],
        )

    with gr.Tab("Reconciliação ATM"):
        build_reconciliation_tab(
            "ATM",
            "Ficheiro ATM / Electronic Journal",
            "Ficheiro Core Banking ATM",
            reconcile_atm,
        )

    with gr.Tab("Reconciliação POS"):
        build_reconciliation_tab(
            "POS",
            "Ficheiro POS",
            "Ficheiro Core Banking POS",
            reconcile_pos,
        )

    with gr.Tab("Reconciliação METIX"):
        build_reconciliation_tab(
            "METIX",
            "Ficheiro METIX",
            "Ficheiro Core Banking METIX",
            reconcile_metix,
        )

    with gr.Tab("Reconciliação Compensação"):
        build_reconciliation_tab(
            "COMPENSAÇÃO",
            "Ficheiro de Compensação",
            "Ficheiro Core Banking Compensação",
            reconcile_compensation,
        )

    with gr.Tab("Gestão de Exceções"):
        gr.Markdown(
            "## Gestão de Exceções",
            elem_classes=["section-title"],
        )

        refresh_exceptions = gr.Button(
            "Atualizar lista de exceções"
        )

        exceptions_table = gr.Dataframe(
            interactive=False,
            wrap=True,
        )

        refresh_exceptions.click(
            load_exceptions,
            outputs=exceptions_table,
        )

        gr.Markdown("### Atualizar uma exceção")

        with gr.Row():
            exception_id = gr.Number(
                label="ID da exceção",
                precision=0,
            )

            exception_status = gr.Dropdown(
                choices=[
                    "PENDENTE",
                    "EM ANÁLISE",
                    "RESOLVIDA",
                    "REJEITADA",
                ],
                value="EM ANÁLISE",
                label="Novo estado",
            )

            responsible = gr.Textbox(
                label="Responsável"
            )

        resolution_comment = gr.Textbox(
            label="Comentário",
            lines=3,
        )

        update_exception_button = gr.Button(
            "Guardar atualização",
            elem_classes=["primary-btn"],
        )

        update_message = gr.Textbox(
            label="Resultado",
            interactive=False,
        )

        update_exception_button.click(
            update_exception,
            inputs=[
                exception_id,
                exception_status,
                responsible,
                resolution_comment,
            ],
            outputs=[
                update_message,
                exceptions_table,
            ],
        )

    with gr.Tab("Relatórios"):
        gr.Markdown(
            "## Relatórios",
            elem_classes=["section-title"],
        )

        refresh_reports = gr.Button(
            "Atualizar relatórios"
        )

        reports_table = gr.Dataframe(
            interactive=False,
            wrap=True,
        )

        refresh_reports.click(
            load_reports,
            outputs=reports_table,
        )

    with gr.Tab("Auditoria e Logs"):
        gr.Markdown(
            "## Auditoria",
            elem_classes=["section-title"],
        )

        refresh_logs = gr.Button(
            "Atualizar logs"
        )

        logs_table = gr.Dataframe(
            interactive=False,
            wrap=True,
        )

        refresh_logs.click(
            load_logs,
            outputs=logs_table,
        )

    with gr.Tab("Administração"):
        gr.Markdown(
            """
            ## Administração

            Área reservada para futura gestão de:

            - utilizadores;
            - perfis;
            - permissões;
            - configurações do sistema.
            """
        )


if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=PORT,
        auth=[
            (
                APP_USERNAME,
                APP_PASSWORD,
            )
        ],
        show_error=True,
    )
