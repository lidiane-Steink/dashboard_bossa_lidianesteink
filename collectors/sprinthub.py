import json
import time
import concurrent.futures

import requests

import config

BASE_URL = "https://sprinthub-api-master.sprinthub.app"
MAX_WORKERS = 6
PAGE_SIZE = 200
# Cap defensivo para evitar loop infinito em caso de bug
MAX_PAGES = 800
# Rate limit do SprintHub (medido): ~50 requisições por janela de ~30s.
# Pausamos a cada 45 páginas por 35s para a janela resetar e conseguir paginar tudo.
RATE_LIMIT_PAGES = 45
RATE_LIMIT_PAUSE = 35

# Mapeamento crm_column ID (número) → nome da etapa do funil.
# Preencha os IDs corretos após verificar no SprintHub:
#   Configurações → CRM → Colunas (anote o ID de cada coluna)
# Enquanto não preencher, etapas aparecem como "coluna_159" no dashboard.
STAGE_MAP: dict[str, str] = json.loads(config.SPRINTHUB_STAGE_MAP or "{}")


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.SPRINTHUB_API_TOKEN}",
        "apitoken": config.SPRINTHUB_API_TOKEN,
    }


def _base_params() -> dict:
    return {"i": config.SPRINTHUB_INSTANCE}


def _resolve_stage(opp: dict) -> str:
    """Retorna o nome da etapa: Comprou, Perdido, ou nome mapeado do crm_column."""
    if opp.get("gain_date") or opp.get("status") == "won":
        return "Comprou"
    if opp.get("lost_date") or opp.get("status") == "lost":
        return "Perdido"
    col_id = str(opp.get("crm_column", ""))
    return STAGE_MAP.get(col_id, f"coluna_{col_id}") if col_id else "sem etapa"


def _get_leads_page(page: int, max_retries: int = 6) -> dict:
    """Busca uma página de leads. Em 401 (rate limit) espera a janela (~30s) resetar."""
    params = {**_base_params(), "allFields": "1", "page": page, "limit": PAGE_SIZE}
    for attempt in range(max_retries):
        try:
            resp = requests.get(f"{BASE_URL}/leads", headers=_headers(), params=params, timeout=30)
            if resp.status_code == 401:
                # Rate limit (50 req / ~30s). Espera a janela resetar e tenta de novo.
                if attempt < max_retries - 1:
                    print(f"    [rate limit] 401 na pág {page} — aguardando {RATE_LIMIT_PAUSE}s p/ resetar...")
                    time.sleep(RATE_LIMIT_PAUSE)
                    continue
                print(f"    ERRO 401 persistente na pág {page}.")
                return {}
            resp.raise_for_status()
            return resp.json().get("data", {})
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            print(f"    ERRO definitivo na pág {page}: {e}")
            return {}
    return {}


def _get_field(lead: dict, *names) -> str:
    """Procura o valor de um campo no lead, tentando múltiplos nomes (case-insensitive)."""
    fields = lead.get("fields") or {}
    for name in names:
        if lead.get(name):
            return lead[name]
        if fields.get(name):
            return fields[name]
        # case-insensitive
        for k, v in fields.items():
            if k.lower() == name.lower() and v:
                return v
    return ""


def get_leads_raw() -> list:
    """Pagina TODOS os leads, pausando p/ respeitar o rate limit (50 req / ~30s)."""
    print("  Coletando leads do SprintHub (com pausas p/ rate limit)...")
    all_leads = []
    page = 1
    reqs_since_pause = 0
    while page <= MAX_PAGES:
        # Pausa preventiva antes de bater o limite de 50 req/janela.
        if reqs_since_pause >= RATE_LIMIT_PAGES:
            print(f"    pausando {RATE_LIMIT_PAUSE}s p/ resetar rate limit "
                  f"({len(all_leads)} leads até a pág {page-1})...")
            time.sleep(RATE_LIMIT_PAUSE)
            reqs_since_pause = 0

        data = _get_leads_page(page)
        reqs_since_pause += 1
        batch = data.get("leads", [])
        if not batch:
            break
        all_leads.extend(batch)
        if page % 25 == 0:
            print(f"    pág {page}: {len(all_leads)} leads coletados...")
        page += 1
        time.sleep(0.2)
    print(f"  Total de leads coletados: {len(all_leads)} (em {page-1} páginas)")
    return all_leads


# SprintHub guarda UTMs num array createdByUtm, nesta ordem (confirmado via API):
#   [0]=utm_source  [1]=utm_campaign  [2]=utm_medium  [3]=utm_term  [4]=utm_content
# Normalizamos o source para as chaves que o dashboard espera (metaads / googlecpc).
SOURCE_NORMALIZE = {
    "facebook":  "metaads",
    "fb":        "metaads",
    "instagram": "metaads",
    "meta":      "metaads",
    "metaads":   "metaads",
    "googleads": "googlecpc",
    "google":    "googlecpc",
    "googlecpc": "googlecpc",
    "adwords":   "googlecpc",
}


def _clean_utm(v) -> str:
    """Limpa um valor de UTM. Macros não renderizadas ({{...}}) viram string vazia."""
    if not v:
        return ""
    s = str(v).strip()
    # Macro do Meta que não foi substituída (ex: "{{leads_site}}") — descarta
    if s.startswith("{{") and s.endswith("}}"):
        return ""
    return s


def _parse_utm_array(lead: dict) -> dict:
    """Extrai utm_* do array createdByUtm do SprintHub."""
    arr = lead.get("createdByUtm")
    if not isinstance(arr, list):
        return {"utm_source": "", "utm_medium": "", "utm_campaign": "",
                "utm_content": "", "utm_term": ""}
    # Preenche com vazio até ter 5 posições
    arr = (list(arr) + ["", "", "", "", ""])[:5]
    raw_source = _clean_utm(arr[0]).lower()
    return {
        "utm_source":   SOURCE_NORMALIZE.get(raw_source, raw_source),
        "utm_campaign": _clean_utm(arr[1]),
        "utm_medium":   _clean_utm(arr[2]),
        "utm_term":     _clean_utm(arr[3]),
        "utm_content":  _clean_utm(arr[4]),
    }


def normalize_leads(raw_leads: list) -> list:
    """Converte leads brutos do SprintHub para o formato do pipeline."""
    leads = []
    for lead in raw_leads:
        # Nome = firstname + lastname (SprintHub guarda separado)
        first = (lead.get("firstname") or "").strip()
        last  = (lead.get("lastname")  or "").strip()
        nome  = f"{first} {last}".strip() or lead.get("fullname") or lead.get("name") or ""

        # Telefone — tenta múltiplos campos
        telefone = lead.get("phone") or lead.get("mobile") or lead.get("whatsapp") or ""

        # "Canal" = como o cliente conheceu a loja (campo customizado do Ouro do Gege)
        canal = lead.get("como_conheceu_a_loja") or _get_field(lead, "canal", "channel", "fonte") or ""

        # Tags / status — SprintHub usa sh_status
        tags = lead.get("sh_status") or ""
        if isinstance(lead.get("tags"), list):
            tags = ",".join(lead["tags"])

        utm = _parse_utm_array(lead)

        leads.append({
            "id":              lead.get("id"),
            "nome":             nome,
            "email":            lead.get("email") or "",
            "telefone":         telefone,
            "whatsapp":         lead.get("whatsapp") or "",
            "criado_em":        lead.get("createDate") or lead.get("created_at"),
            "atualizado_em":    lead.get("updatedDate") or "",
            "ultimo_acesso":    lead.get("lastActive") or "",
            "responsavel":      lead.get("createdByName") or lead.get("updatedByName") or "",
            "modelo_interesse": lead.get("modelos_de_interesse") or "",
            "bairro":           lead.get("bairro") or "",
            "cidade":           lead.get("city") or "",
            "estado":           lead.get("state") or "",
            "genero":           lead.get("genero") or "",
            "possui_filhos":    lead.get("possui_filhos") or False,
            "possui_pets":      lead.get("possui_pets") or False,
            "canal":            canal,
            "tags":             tags,
            "stage":            lead.get("stage") or "",
            "points":           lead.get("points") or 0,
            # UTMs extraídas do array createdByUtm do SprintHub
            "utm_source":       utm["utm_source"],
            "utm_medium":       utm["utm_medium"],
            "utm_campaign":     utm["utm_campaign"],
            "utm_content":      utm["utm_content"],
            "utm_term":         utm["utm_term"],
        })

    total_bruto = len(leads)

    # 1) Filtra visitantes anônimos: o SprintHub cria um "lead" para cada visita ao
    #    site (só com cidade por IP). Mantemos só quem tem contato real.
    leads = [l for l in leads
             if (l.get("nome") or l.get("telefone") or l.get("whatsapp") or l.get("email"))]
    anonimos = total_bruto - len(leads)

    # 2) Dedupe por contato (mesma pessoa em registros diferentes) — mantém o mais recente.
    dedup = {}
    for l in leads:
        key = (str(l.get("whatsapp") or l.get("telefone") or l.get("email") or l.get("id"))
               .strip().lower())
        prev = dedup.get(key)
        if prev is None or (l.get("criado_em") or "") > (prev.get("criado_em") or ""):
            dedup[key] = l
    leads_final = list(dedup.values())
    duplicados = len(leads) - len(leads_final)

    print(f"  Leads: {total_bruto} brutos → {anonimos} anônimos removidos, "
          f"{duplicados} duplicados removidos → {len(leads_final)} leads reais.")
    return leads_final


def _get_opportunities(lead_id: int) -> list:
    try:
        resp = requests.get(
            f"{BASE_URL}/listopportunitysleadcomplete/{lead_id}",
            headers=_headers(),
            params=_base_params(),
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        return result if isinstance(result, list) else []
    except Exception:
        return []


def get_all_leads() -> list:
    """Wrapper: coleta + normaliza."""
    return normalize_leads(get_leads_raw())


def get_deals_from_leads(leads: list) -> list:
    """Busca oportunidades a partir de uma lista de leads JÁ COLETADA (evita refetch)."""
    print("Coletando oportunidades (deals) do SprintHub...")
    lead_by_id = {lead.get("id"): lead for lead in leads}
    lead_ids = list(lead_by_id.keys())

    print(f"  Buscando oportunidades para {len(lead_ids)} leads (workers={MAX_WORKERS})...")
    deals = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_get_opportunities, lid): lid for lid in lead_ids}
        for future in concurrent.futures.as_completed(futures):
            lead_id = futures[future]
            lead = lead_by_id.get(lead_id, {})
            for opp in future.result():
                deals.append({
                    "id":               opp.get("id"),
                    "nome":             opp.get("title"),
                    "valor_total":      opp.get("value"),
                    "ganho":            bool(opp.get("gain_date") or opp.get("status") == "won"),
                    "data_fechamento":  opp.get("gain_date") or opp.get("lost_date"),
                    "data_previsao":    opp.get("expectedCloseDate"),
                    "criado_em":        opp.get("createDate"),
                    "atualizado_em":    opp.get("updateDate"),
                    "etapa":            _resolve_stage(opp),
                    "pipeline":         None,
                    "responsavel":      (opp.get("user") or {}).get("name"),
                    "motivo_perda":     opp.get("loss_reason"),
                    "contato_nome":     lead.get("nome"),
                    "contato_email":    lead.get("email"),
                    "contato_telefone": lead.get("telefone") or lead.get("whatsapp"),
                    "em_espera":        False,
                })
            done += 1
            if done % 100 == 0:
                print(f"    {done}/{len(lead_ids)} leads processados...")

    print(f"  Total de deals: {len(deals)}")
    return deals


def get_all_crm_data() -> dict:
    """Wrapper antigo — coleta leads e depois deals. Mantido para compatibilidade."""
    raw_leads = get_leads_raw()
    return {
        "deals":      get_deals_from_leads(raw_leads),
        "atividades": [],
        "tarefas":    [],
    }
