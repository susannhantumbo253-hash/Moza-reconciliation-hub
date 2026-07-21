# Moza Reconcile — MVP ATM

## Funcionalidades
- Login protegido
- Upload ATM/EJ e Core Banking
- Validação de CSV/Excel
- Comparação por referência e montante
- Exceções, duplicados, dashboard
- Excel, CSV e logs de auditoria

## Executar localmente
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
Login inicial: `admin` / `Moza@12345`

## Publicar no Render
- Build Command: `pip install -r requirements.txt`
- Start Command: `python app.py`
- Environment variables: `APP_USERNAME` e `APP_PASSWORD`

Não carregue dados bancários reais numa hospedagem pública. A produção deve usar infraestrutura aprovada pelo banco.
