"""
Coletor que lê dados de Meta Ads e Google Ads das abas 'Meta' e 'Google'
da MESMA planilha principal do cliente (GOOGLE_SHEET_ID no .env).

Estrutura esperada:
─ Aba 'Google' ────────────────────────────────────────────────────────
  A: Data | B: Anúncio | C: Nome da Campanha | D: Grupo de Anúncio
  E: Conversões | F: Custo por Todas | G: Impressões | H: Cliques
  I: CPC Médio | J: CTR | K: Custo (spend total)

─ Aba 'Meta' ──────────────────────────────────────────────────────────
  A: Data | B: Anúncio | C: Conjunto de Anúncios | D: Nome da Campanha
  E: Leads do Website | F: Custo por lead | G: Impressões | H: Alcance
  I: Cliques | J: CPC Médio | K: CPM | L: CTR | M: Valor Investido
"""
import gspread
from google.oauth2.service_account import Credentials
import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _get_client():
    creds = Credentials.from_service_account_file(
        config.GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
    )
    return gspread.authorize(creds)


def _parse_number(value):
    if value is None or value == "":
        return None
    try:
        s = str(value).replace("R$", "").replace("%", "").replace(" ", "").strip()
        # Remove separador de milhar (ponto antes de vírgula), troca vírgula por ponto
        if "," in s and "." in s:
            # ex: "1.234,56" → "1234.56"
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", ".")
        return float(s)
    except Exception:
        return None


def _normalize_date(date_str):
    """Converte DD-MM-YYYY ou DD/MM/YYYY para YYYY-MM-DD (formato ISO)."""
    if not date_str:
        return ""
    s = str(date_str).strip()
    # Já está no formato ISO
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    # DD-MM-YYYY ou DD/MM/YYYY
    sep = "-" if "-" in s else ("/" if "/" in s else None)
    if sep:
        parts = s.split(sep)
        if len(parts) == 3 and len(parts[2]) == 4:
            d, m, y = parts
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    return s


def _read_tab(tab_name: str) -> list[list[str]]:
    gc = _get_client()
    spreadsheet = gc.open_by_key(config.GOOGLE_SHEET_ID)
    try:
        ws = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        print(f"  AVISO: aba '{tab_name}' não encontrada.")
        return []
    return ws.get_all_values()


def get_google_ads_sheet_data() -> list[dict]:
    """Lê aba 'Google' e retorna dados normalizados (1 linha = 1 dia x anúncio)."""
    print("Coletando Google Ads da planilha (aba 'Google')...")
    rows = _read_tab("Google")
    if len(rows) < 2:
        return []

    data = []
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        # Garante 11 colunas
        while len(row) < 11:
            row.append("")
        data.append({
            "data":       _normalize_date(row[0]),
            "anuncio":    row[1],
            "campanha":   row[2],
            "grupo":      row[3],
            "conversoes": _parse_number(row[4]) or 0,
            # row[5] = "Custo por Todas" — métrica calculada (CPA), ignoramos
            "impressoes": _parse_number(row[6]) or 0,
            "cliques":    _parse_number(row[7]) or 0,
            "cpc":        _parse_number(row[8]) or 0,
            "ctr":        _parse_number(row[9]) or 0,
            "gasto":      _parse_number(row[10]) or 0,  # coluna K: Custo total
        })
    print(f"  {len(data)} linhas de Google Ads lidas.")
    return data


def get_meta_ads_sheet_data() -> list[dict]:
    """Lê aba 'Meta' e retorna dados normalizados (1 linha = 1 dia x anúncio)."""
    print("Coletando Meta Ads da planilha (aba 'Meta')...")
    rows = _read_tab("Meta")
    if len(rows) < 2:
        return []

    data = []
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        while len(row) < 13:
            row.append("")
        data.append({
            "data":         _normalize_date(row[0]),
            "anuncio":      row[1],
            "conjunto":     row[2],
            "campanha":     row[3],
            "leads":        _parse_number(row[4]) or 0,
            # row[5] = Custo por lead (CPL — calculado), ignoramos
            "impressoes":   _parse_number(row[6]) or 0,
            "alcance":      _parse_number(row[7]) or 0,
            "cliques_link": _parse_number(row[8]) or 0,
            "cpc":          _parse_number(row[9]) or 0,
            "cpm":          _parse_number(row[10]) or 0,
            "ctr":          _parse_number(row[11]) or 0,
            "gasto":        _parse_number(row[12]) or 0,  # coluna M: Valor Investido
            # Campos opcionais usados pelo dashboard (vazios — não temos)
            "thumbnail":    "",
            "permalink":    "",
            "data_criacao": "",
        })
    print(f"  {len(data)} linhas de Meta Ads lidas.")
    return data


# ─── Compatibilidade com nomes antigos ───────────────────────────────────────
def get_ads_data():
    """Retorna (meta_ads, google_ads) — assinatura antiga mantida para compatibilidade."""
    return get_meta_ads_sheet_data(), get_google_ads_sheet_data()


def get_google_ads_creatives_sheet_data():
    """Placeholder — Ouro do Gege ainda não tem aba de criativos separada."""
    print("  Aba de criativos do Google Ads não configurada — pulando.")
    return []
