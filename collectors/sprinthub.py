import json
import time
import concurrent.futures

import requests

import config

BASE_URL = "https://sprinthub-api-master.sprinthub.app"
MAX_WORKERS = 6
# API SprintHub limita ~30 por página com allFields=1, ~100 sem. Mantemos alto e
# paramos quando a página vier curta (lógica abaixo).
PAGE_SIZE = 200
# Cap defensivo para evitar loop infinito em caso de bug
MAX_PAGES = 500

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


def _get_leads_page(page: int) -> dict:
    params = {**_base_params(), "allFields": "1", "page": page, "limit": PAGE_SIZE}
    resp = requests.get(f"{BASE_URL}/leads", headers=_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", {})


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
    """Retorna leads no formato bruto do SprintHub (paginação até esgotar)."""
    print("  Coletando leads do SprintHub...")
    all_leads = []
    page = 1
    last_batch_size = 0
    while page <= MAX_PAGES:
        data = _get_leads_page(page)
        batch = data.get("leads", [])
        if not batch:
            break
        all_leads.extend(batch)
        total = data.get("total") or 0
        # Log de progresso a cada 5 páginas para não poluir
        if page == 1 or page % 5 == 0:
            tag = f"/{total}" if total else ""
            print(f"    pág {page}: +{len(batch)} leads → {len(all_leads)}{tag} no total")
        # Critérios de parada (em ordem de prioridade):
        # 1) total conhecido e já atingido
        # 2) batch curto (menos que o anterior ou < 10) = última página
        # 3) cap MAX_PAGES atingido
        if total and len(all_leads) >= total:
            break
        if last_batch_size and len(batch) < last_batch_size and len(batch) < 10:
            break
        last_batch_size = len(batch)
        page += 1
        time.sleep(0.15)
    print(f"  Total de leads coletados: {len(all_leads)}")
    return all_leads


def get_all_leads() -> list:
    """Retorna leads no formato esperado pelo pipeline."""
    raw_leads = get_leads_raw()
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
            # Campos UTM mantidos para compatibilidade com o resto do pipeline (vazios se não houver)
            "utm_source":       _get_field(lead, "utm_source", "utmSource"),
            "utm_medium":       _get_field(lead, "utm_medium", "utmMedium"),
            "utm_campaign":     _get_field(lead, "utm_campaign", "utmCampaign"),
            "utm_content":      _get_field(lead, "utm_content", "utmContent"),
            "utm_term":         _get_field(lead, "utm_term", "utmTerm"),
        })
    return leads


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


def get_deals() -> list:
    print("Coletando oportunidades (deals) do SprintHub...")
    leads = get_leads_raw()
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
                    "contato_nome":     lead.get("fullname"),
                    "contato_email":    lead.get("email"),
                    "contato_telefone": lead.get("phone"),
                    "em_espera":        False,
                })
            done += 1
            if done % 100 == 0:
                print(f"    {done}/{len(lead_ids)} leads processados...")

    print(f"  Total de deals: {len(deals)}")
    return deals


def get_all_crm_data() -> dict:
    return {
        "deals":      get_deals(),
        "atividades": [],
        "tarefas":    [],
    }
