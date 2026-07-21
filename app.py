import os
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path

import gradio as gr
import pandas as pd
import matplotlib.pyplot as plt


# =========================================================
# CONFIGURAÇÕES
# =========================================================

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
OUTPUT_DIR = APP_DIR / "outputs"

DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "audit.db"

REQUIRED_COLUMNS = [
    "reference",
    "transaction_date",
    "amount",
]


# =========================================================
# ESTILO DA APLICAÇÃO
# =========================================================

CSS = """
:root {
    --moza-red: #c8102e;
    --moza-dark: #8f0b20;
    --moza-light: #fff4f5;
    --moza-grey: #f4f5f7;
}

.gradio-container {
    max-width: 1500px !important;
}

#topbar {
    background: linear-gradient(
        90deg,
        var(--moza-dark),
        var(--moza-red)
    );
    color: white;
    padding: 22px 28px;
    border-radius: 14px;
    margin-bottom: 16px;
}

#topbar h1,
#topbar p {
    color: white !important;
    margin: 0;
}

.primary-btn {
    background: var(--moza-red) !important;
    color: white !important;
    border: none !important;
}

.metric-card {
    border-left: 5px solid var(--moza-red);
    background: var(--moza-light);
    padding: 15px 18px;
    border-radius: 10px;
    margin-bottom: 8px;
}

.dashboard-card {
    background: white;
    border: 1px solid #e4e4e4;
    padding: 18px;
    border-radius: 12px;
    min-height: 110px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
}

.dashboard-card h3 {
    color: #666 !important;
    font-size: 15px;
    margin: 0;
}

.dashboard-card h2 {
    color: var(--moza-red) !important;
    font-size: 28px;
    margin-top: 8px;
}

.section-title {
    border-bottom: 2px solid var(--moza-red);
    padding-bottom: 6px;
}

footer {
    display: none !important;
}
"""


# =========================================================
# BASE DE DADOS
# =========================================================

def init_db():
    with sqlite3.connect(DB_PATH) as conn:

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT
            )
            """
        )

        conn.execute(
            """
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
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS exceptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                process_type TEXT NOT NULL,
                reference TEXT,
                exception_type TEXT NOT NULL,
                amount_atm REAL,
                amount_core REAL,
                amount_difference REAL,
                status TEXT NOT NULL DEFAULT 'PENDENTE',
                assigned_to TEXT,
                resolution_comment TEXT,
                resolved_at TEXT,
                FOREIGN KEY(run_id)
                    REFERENCES reconciliation_runs(id)
            )
            """
        )

        conn.commit()


def write_log(username, action, details=""):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO audit_logs(
                created_at,
                username,
                action,
                details
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                username or "desconhecido",
                action,
                details,
            ),
        )
        conn.commit()


# =========================================================
# LEITURA E VALIDAÇÃO DE FICHEIROS
# =========================================================

def file_hash(path):
    hash_object = hashlib.sha256()

    with open(path, "rb") as file:
        for chunk in iter(
            lambda: file.read(1024 * 1024),
            b"",
        ):
            hash_object.update(chunk)

    return hash_object.hexdigest()


def read_table(file_path):
    suffix = Path(file_path).suffix.lower()

    if suffix == ".csv":
        try:
            return pd.read_csv(file_path)
        except UnicodeDecodeError:
            return pd.read_csv(
                file_path,
                encoding="latin-1",
            )

    if suffix in [".xlsx", ".xls"]:
        return pd.read_excel(file_path)

    raise ValueError(
        "Formato não suportado. "
        "Use CSV, XLSX ou XLS."
    )


def normalize(dataframe, source_name):
    dataframe = dataframe.copy()

    dataframe.columns = [
        str(column).strip().lower()
        for column in dataframe.columns
    ]

    missing_columns = [
        column
        for column in REQUIRED_COLUMNS
        if column not in dataframe.columns
    ]

    if missing_columns:
        raise ValueError(
            f"O ficheiro {source_name} não contém: "
            f"{', '.join(missing_columns)}"
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

    dataframe["row_number"] = range(
        2,
        len(dataframe) + 2,
    )

    invalid_rows = dataframe[
        dataframe["reference"].eq("")
        | dataframe["transaction_date"].isna()
        | dataframe["amount"].isna()
    ]

    if not invalid_rows.empty:
        raise ValueError(
            f"O ficheiro {source_name} possui "
            f"{len(invalid_rows)} linha(s) inválida(s)."
        )

    return dataframe


# =========================================================
# CLASSIFICAÇÃO
# =========================================================

def classify_transaction(row):
    if row["_merge"] == "left_only":
        return "EXCEÇÃO: APENAS ATM/EJ"

    if row["_merge"] == "right_only":
        return "EXCEÇÃO: APENAS CORE"

    if (
        bool(row.get("duplicate_atm", False))
        or bool(row.get("duplicate_core", False))
    ):
        return "EXCEÇÃO: DUPLICADO"

    if round(float(row["amount_atm"]), 2) != round(
        float(row["amount_core"]),
        2,
    ):
        return "EXCEÇÃO: MONTANTE DIFERENTE"

    return "RECONCILIADO"


# =========================================================
# GUARDAR EXECUÇÃO E EXCEÇÕES
# =========================================================

def save_reconciliation_run(
    username,
    total,
    reconciled,
    exceptions,
    rate,
    exception_value,
    excel_path,
    csv_path,
    result,
):
    created_at = datetime.now().isoformat(
        timespec="seconds"
    )

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO reconciliation_runs(
                created_at,
                username,
                process_type,
                total_records,
                reconciled_records,
                exception_records,
                reconciliation_rate,
                exception_value,
                excel_report,
                csv_report
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                username,
                "ATM",
                total,
                reconciled,
                exceptions,
                rate,
                exception_value,
                str(excel_path),
                str(csv_path),
            ),
        )

        run_id = cursor.lastrowid

        exception_rows = result[
            result["reconciliation_status"]
            != "RECONCILIADO"
        ]

        for _, row in exception_rows.iterrows():
            cursor.execute(
                """
                INSERT INTO exceptions(
                    run_id,
                    created_at,
                    process_type,
                    reference,
                    exception_type,
                    amount_atm,
                    amount_core,
                    amount_difference,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    created_at,
                    "ATM",
                    str(row.get("reference", "")),
                    str(
                        row.get(
                            "reconciliation_status",
                            "",
                        )
                    ),
                    safe_float(row.get("amount_atm")),
                    safe_float(row.get("amount_core")),
                    safe_float(
                        row.get("amount_difference")
                    ),
                    "PENDENTE",
                ),
            )

        conn.commit()

    return run_id


def safe_float(value):
    if pd.isna(value):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# =========================================================
# MOTOR DE RECONCILIAÇÃO ATM
# =========================================================

def reconcile(source_file, target_file, request: gr.Request):
    username = (
        getattr(request, "username", None)
        or "utilizador"
    )

    if not source_file or not target_file:
        raise gr.Error(
            "Carregue os dois ficheiros antes "
            "de executar a reconciliação."
        )

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

    try:
        source = normalize(
            read_table(source_path),
            "ATM/EJ",
        )

        target = normalize(
            read_table(target_path),
            "CORE",
        )

    except Exception as error:
        write_log(
            username,
            "FALHA_VALIDACAO",
            str(error),
        )

        raise gr.Error(str(error))

    source["duplicate"] = source.duplicated(
        subset=["reference", "amount"],
        keep=False,
    )

    target["duplicate"] = target.duplicated(
        subset=["reference", "amount"],
        keep=False,
    )

    left = source.rename(
        columns={
            "transaction_date": "date_atm",
            "amount": "amount_atm",
            "status": "status_atm",
            "row_number": "row_atm",
            "duplicate": "duplicate_atm",
        }
    )

    right = target.rename(
        columns={
            "transaction_date": "date_core",
            "amount": "amount_core",
            "status": "status_core",
            "row_number": "row_core",
            "duplicate": "duplicate_core",
        }
    )

    result = left.merge(
        right[
            [
                "reference",
                "date_core",
                "amount_core",
                "status_core",
                "row_core",
                "duplicate_core",
            ]
        ],
        on="reference",
        how="outer",
        indicator=True,
    )

    result["reconciliation_status"] = result.apply(
        classify_transaction,
        axis=1,
    )

    result["amount_difference"] = (
        result["amount_atm"].fillna(0)
        - result["amount_core"].fillna(0)
    ).round(2)

    result_columns = [
        "reference",
        "date_atm",
        "date_core",
        "amount_atm",
        "amount_core",
        "amount_difference",
        "status_atm",
        "status_core",
        "reconciliation_status",
        "row_atm",
        "row_core",
    ]

    result = (
        result[result_columns]
        .sort_values(
            [
                "reconciliation_status",
                "reference",
            ]
        )
        .reset_index(drop=True)
    )

    total = len(result)

    reconciled = int(
        (
            result["reconciliation_status"]
            == "RECONCILIADO"
        ).sum()
    )

    exceptions = total - reconciled

    rate = (
        reconciled / total * 100
        if total
        else 0
    )

    exception_value = float(
        result.loc[
            result["reconciliation_status"]
            != "RECONCILIADO",
            "amount_difference",
        ]
        .abs()
        .sum()
    )

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    excel_path = OUTPUT_DIR / (
        f"reconciliacao_atm_{timestamp}.xlsx"
    )

    csv_path = OUTPUT_DIR / (
        f"reconciliacao_atm_{timestamp}.csv"
    )

    with pd.ExcelWriter(
        excel_path,
        engine="openpyxl",
    ) as writer:

        result.to_excel(
            writer,
            sheet_name="Resultado",
            index=False,
        )

        result[
            result["reconciliation_status"]
            != "RECONCILIADO"
        ].to_excel(
            writer,
            sheet_name="Excecoes",
            index=False,
        )

        summary_data = pd.DataFrame(
            [
                {
                    "total_registos": total,
                    "reconciliados": reconciled,
                    "excecoes": exceptions,
                    "taxa_reconciliacao_percentagem":
                        round(rate, 2),
                    "valor_absoluto_diferencas":
                        round(exception_value, 2),
                    "hash_ficheiro_atm":
                        file_hash(source_path),
                    "hash_ficheiro_core":
                        file_hash(target_path),
                    "executado_por": username,
                    "executado_em":
                        datetime.now().isoformat(
                            timespec="seconds"
                        ),
                }
            ]
        )

        summary_data.to_excel(
            writer,
            sheet_name="Resumo",
            index=False,
        )

    result.to_csv(
        csv_path,
        index=False,
    )

    save_reconciliation_run(
        username=username,
        total=total,
        reconciled=reconciled,
        exceptions=exceptions,
        rate=rate,
        exception_value=exception_value,
        excel_path=excel_path,
        csv_path=csv_path,
        result=result,
    )

    counts = (
        result["reconciliation_status"]
        .value_counts()
    )

    figure, axis = plt.subplots(
        figsize=(8, 4.5)
    )

    counts.plot(
        kind="bar",
        ax=axis,
    )

    axis.set_title(
        "Resultado da Reconciliação ATM"
    )

    axis.set_xlabel("")
    axis.set_ylabel("Quantidade")

    axis.tick_params(
        axis="x",
        rotation=25,
    )

    figure.tight_layout()

    summary_html = f"""
    <div class="metric-card">
        <b>Total analisado:</b> {total:,}<br>
        <b>Reconciliado:</b> {reconciled:,}<br>
        <b>Exceções:</b> {exceptions:,}<br>
        <b>Taxa de reconciliação:</b>
        {rate:.2f}%<br>
        <b>Valor absoluto das diferenças:</b>
        {exception_value:,.2f} MZN
    </div>
    """

    write_log(
        username,
        "RECONCILIACAO_ATM",
        (
            f"Total={total}; "
            f"Reconciliados={reconciled}; "
            f"Excecoes={exceptions}"
        ),
    )

    return (
        summary_html,
        result,
        figure,
        str(excel_path),
        str(csv_path),
    )


# =========================================================
# DASHBOARD
# =========================================================

def load_dashboard():
    with sqlite3.connect(DB_PATH) as conn:

        run_data = pd.read_sql_query(
            """
            SELECT
                COUNT(*) AS total_runs,
                COALESCE(
                    SUM(total_records),
                    0
                ) AS total_records,
                COALESCE(
                    SUM(reconciled_records),
                    0
                ) AS reconciled_records,
                COALESCE(
                    SUM(exception_records),
                    0
                ) AS exception_records
            FROM reconciliation_runs
            """,
            conn,
        ).iloc[0]

        pending_data = pd.read_sql_query(
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
                ROUND(
                    reconciliation_rate,
                    2
                ) AS taxa_percentagem
            FROM reconciliation_runs
            ORDER BY id DESC
            LIMIT 20
            """,
            conn,
        )

    total_records = int(
        run_data["total_records"]
    )

    reconciled_records = int(
        run_data["reconciled_records"]
    )

    exception_records = int(
        run_data["exception_records"]
    )

    total_runs = int(
        run_data["total_runs"]
    )

    general_rate = (
        reconciled_records
        / total_records
        * 100
        if total_records
        else 0
    )

    dashboard_html = f"""
    <div style="
        display:grid;
        grid-template-columns:
        repeat(auto-fit,minmax(190px,1fr));
        gap:12px;
    ">

        <div class="dashboard-card">
            <h3>Execuções</h3>
            <h2>{total_runs:,}</h2>
        </div>

        <div class="dashboard-card">
            <h3>Transações analisadas</h3>
            <h2>{total_records:,}</h2>
        </div>

        <div class="dashboard-card">
            <h3>Reconciliadas</h3>
            <h2>{reconciled_records:,}</h2>
        </div>

        <div class="dashboard-card">
            <h3>Exceções encontradas</h3>
            <h2>{exception_records:,}</h2>
        </div>

        <div class="dashboard-card">
            <h3>Exceções pendentes</h3>
            <h2>{int(pending_data):,}</h2>
        </div>

        <div class="dashboard-card">
            <h3>Taxa geral</h3>
            <h2>{general_rate:.2f}%</h2>
        </div>

    </div>
    """

    return dashboard_html, history


# =========================================================
# GESTÃO DE EXCEÇÕES
# =========================================================

def load_exceptions():
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(
            """
            SELECT
                id,
                created_at AS data,
                process_type AS processo,
                reference AS referencia,
                exception_type AS tipo_excecao,
                amount_atm AS valor_atm,
                amount_core AS valor_core,
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


def resolve_exception(
    exception_id,
    new_status,
    assigned_to,
    comment,
    request: gr.Request,
):
    username = (
        getattr(request, "username", None)
        or "utilizador"
    )

    if exception_id is None:
        raise gr.Error(
            "Informe o ID da exceção."
        )

    exception_id = int(exception_id)

    resolved_at = None

    if new_status in [
        "RESOLVIDA",
        "REJEITADA",
    ]:
        resolved_at = datetime.now().isoformat(
            timespec="seconds"
        )

    with sqlite3.connect(DB_PATH) as conn:
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
            raise gr.Error(
                "Não foi encontrada uma exceção "
                "com esse ID."
            )

        conn.commit()

    write_log(
        username,
        "ATUALIZACAO_EXCECAO",
        (
            f"Excecao={exception_id}; "
            f"Estado={new_status}"
        ),
    )

    message = (
        f"Exceção {exception_id} atualizada "
        f"para {new_status}."
    )

    return message, load_exceptions()


# =========================================================
# LOGS E HISTÓRICO
# =========================================================

def load_logs(request: gr.Request):
    username = (
        getattr(request, "username", None)
        or "utilizador"
    )

    write_log(
        username,
        "CONSULTA_LOGS",
        "Consulta ao histórico de auditoria",
    )

    with sqlite3.connect(DB_PATH) as conn:
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


def load_reports():
    with sqlite3.connect(DB_PATH) as conn:
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
                ROUND(
                    reconciliation_rate,
                    2
                ) AS taxa_percentagem,
                ROUND(
                    exception_value,
                    2
                ) AS valor_diferencas
            FROM reconciliation_runs
            ORDER BY id DESC
            LIMIT 200
            """,
            conn,
        )


# =========================================================
# INTERFACE GRADIO
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

        gr.Markdown(
            "### Histórico recente de reconciliações"
        )

        dashboard_history = gr.Dataframe(
            interactive=False,
            wrap=True,
        )

        refresh_dashboard.click(
            load_dashboard,
            outputs=[
                dashboard_cards,
                dashboard_history,
            ],
        )

    with gr.Tab("Reconciliação ATM"):

        gr.Markdown(
            """
            ### Carregamento de ficheiros

            Os dois ficheiros devem conter:

            `reference`, `transaction_date`, `amount`

            A coluna `status` é opcional.
            """
        )

        with gr.Row():
            source_file = gr.File(
                label=(
                    "Ficheiro ATM / "
                    "Electronic Journal"
                ),
                file_types=[
                    ".csv",
                    ".xlsx",
                    ".xls",
                ],
                type="filepath",
            )

            target_file = gr.File(
                label="Ficheiro Core Banking",
                file_types=[
                    ".csv",
                    ".xlsx",
                    ".xls",
                ],
                type="filepath",
            )

        run_button = gr.Button(
            "Executar reconciliação",
            elem_classes=["primary-btn"],
        )

        reconciliation_summary = gr.HTML()

        reconciliation_results = gr.Dataframe(
            label="Resultado detalhado",
            interactive=False,
            wrap=True,
        )

        reconciliation_chart = gr.Plot(
            label="Dashboard ATM"
        )

        with gr.Row():
            excel_download = gr.File(
                label="Relatório Excel"
            )

            csv_download = gr.File(
                label="Resultado CSV"
            )

        run_button.click(
            reconcile,
            inputs=[
                source_file,
                target_file,
            ],
            outputs=[
                reconciliation_summary,
                reconciliation_results,
                reconciliation_chart,
                excel_download,
                csv_download,
            ],
        )

    with gr.Tab("Gestão de Exceções"):

        gr.Markdown(
            """
            ## Exceções

            Consulte as diferenças identificadas
            durante as reconciliações.
            """
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

        gr.Markdown(
            "### Atualizar uma exceção"
        )

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

            exception_responsible = gr.Textbox(
                label="Responsável"
            )

        exception_comment = gr.Textbox(
            label="Comentário da resolução",
            lines=3,
        )

        update_exception_button = gr.Button(
            "Guardar atualização",
            elem_classes=["primary-btn"],
        )

        exception_message = gr.Textbox(
            label="Resultado",
            interactive=False,
        )

        update_exception_button.click(
            resolve_exception,
            inputs=[
                exception_id,
                exception_status,
                exception_responsible,
                exception_comment,
            ],
            outputs=[
                exception_message,
                exceptions_table,
            ],
        )

    with gr.Tab("Relatórios"):

        gr.Markdown(
            "## Histórico de Relatórios"
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
            "## Registo de Atividades"
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

    with gr.Tab("Outros Módulos"):

        gr.Markdown(
            """
            ## Módulos em preparação

            - POS
            - METIX
            - Compensação
            - Interoperabilidade
            - Conta Float
            - Conta Real Time
            - Maker–Checker
            - Envio automático de relatórios
            - Integração com Power Automate
            - Integração com Power BI
            """
        )


# =========================================================
# EXECUÇÃO
# =========================================================

if __name__ == "__main__":

    username = os.getenv(
        "APP_USERNAME",
        "admin",
    )

    password = os.getenv(
        "APP_PASSWORD",
        "Moza@12345",
    )

    port = int(
        os.getenv(
            "PORT",
            "7860",
        )
    )

    demo.launch(
        server_name="0.0.0.0",
        server_port=port,
        auth=[
            (
                username,
                password,
            )
        ],
        show_error=True,
    )
