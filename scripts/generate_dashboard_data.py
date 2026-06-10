#!/usr/bin/env python3
"""
Generate aggregated JSON data for the marketing dashboard.
Run after main.py. Reads from Google Sheets and writes to dashboard/data/summary.json.
"""

import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Conceito "Repasse" era do cliente antigo (franquia / cidades específicas).
# Bossa Elétricas não usa esse modelo — lista vazia = todas as campanhas são normais.
REPASSE_KEYWORDS = []

OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dashboard", "data"
)


def get_client():
    creds = Credentials.from_service_account_file(
        config.GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
    )
    return gspread.authorize(creds)


def read_sheet(gc, sheet_name):
    spreadsheet = gc.open_by_key(config.GOOGLE_SHEET_ID)
    try:
        ws = spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        print(f"  AVISO: aba '{sheet_name}' não existe — retornando lista vazia.")
        return []
    rows = ws.get_all_values()
    if not rows:
        return []
    headers = rows[0]
    return [dict(zip(headers, row)) for row in rows[1:] if any(cell.strip() for cell in row)]


# ════════════════════════════════════════════════════════════════════
# MATCHING utm_content (CRM) ↔ anuncio (Meta Ads)
# ════════════════════════════════════════════════════════════════════
_TOKEN_STOP = {
    'anuncio', 'ad', 'ads', 'v1', 'v2', 'v3', 'v4', 'v5',
    'de', 'da', 'do', 'a', 'o', 'e', 'com', 'para', 'no', 'na', 'os', 'as',
    'estatico', 'animado', 'video', 'vídeo',
}

def _tokens(s):
    """Normaliza string em conjunto de tokens significativos (sem acento, lowercase, sem stop words)."""
    if not s:
        return set()
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii').lower()
    parts = re.split(r'[^a-z0-9]+', s)
    return {p for p in parts if p and p not in _TOKEN_STOP and len(p) >= 3}

def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def match_rd_leads_daily(meta_rows, google_creatives_rows, leads, threshold=0.4):
    """
    Faz matching token-based (Jaccard) entre utm_content de leads do RD Station
    e nomes de anúncios (Meta + Google). Retorna dicts diários.

    Como funciona:
      • Constroi pools separados de anúncios Meta e Google
      • Para cada lead com utm_content, decide o pool baseado em utm_source
      • Encontra melhor anúncio por Jaccard >= threshold
      • Atribui +1 ao (anúncio, data_de_criacao_do_lead)

    Returns:
      meta_daily:   {anuncio: {data: count}}
      google_daily: {anuncio: {data: count}}
      stats: {"meta_matched": N, "google_matched": N, "unmatched": N}
    """
    meta_pool = {(r.get("anuncio") or "").strip() for r in meta_rows}
    meta_pool.discard("")
    google_pool = {(r.get("anuncio") or "").strip() for r in google_creatives_rows}
    google_pool.discard("")

    meta_tokens   = [(a, _tokens(a)) for a in meta_pool]
    google_tokens = [(a, _tokens(a)) for a in google_pool]

    meta_daily   = defaultdict(lambda: defaultdict(int))
    google_daily = defaultdict(lambda: defaultdict(int))
    stats = {"meta_matched": 0, "google_matched": 0, "unmatched": 0}

    for lead in leads:
        utm_c = (lead.get("utm_content") or "").strip()
        if not utm_c:
            continue
        tc = _tokens(utm_c)
        if not tc:
            stats["unmatched"] += 1
            continue

        src = (lead.get("utm_source") or "").strip()
        day = parse_date_to_day(lead.get("criado_em"))
        if not day:
            continue

        if src == "metaads":
            pool, daily, key = meta_tokens, meta_daily, "meta_matched"
        elif src == "googlecpc":
            pool, daily, key = google_tokens, google_daily, "google_matched"
        else:
            continue

        best, best_score = None, 0.0
        for anuncio, ta in pool:
            s = _jaccard(tc, ta)
            if s > best_score:
                best_score, best = s, anuncio

        if best_score >= threshold and best:
            daily[best][day] += 1
            stats[key] += 1
        else:
            stats["unmatched"] += 1

    print(f"  [Match RD/Ads] Meta={stats['meta_matched']}  Google={stats['google_matched']}  sem_match={stats['unmatched']}")
    return dict(meta_daily), dict(google_daily), stats


def attach_rd_leads_to_creatives(creatives_list, meta_rows, leads, threshold=0.4):
    """Conta leads do CRM por criativo via matching token-based de utm_content ↔ anuncio.

    Estratégia:
      1. Constrói pool com TODOS os anúncios da aba meta_ads (não só os top 50)
      2. Para cada lead com utm_source=metaads e utm_content, encontra anuncio
         com maior Jaccard >= threshold
      3. Acumula contagem por anuncio
      4. Anexa leads_rd e cpl_rd em cada item de creatives_list
    """
    # 1. Pool de TODOS os anúncios (do meta_ads raw)
    all_anuncios = set()
    for r in meta_rows:
        a = (r.get("anuncio") or "").strip()
        if a:
            all_anuncios.add(a)
    # Garante que os top creatives também estão no pool
    for c in creatives_list:
        all_anuncios.add(c["anuncio"])
    anuncio_tokens = [(a, _tokens(a)) for a in all_anuncios]

    # 2. Para cada lead metaads, achar melhor anuncio
    leads_per_anuncio = defaultdict(int)
    unmatched = 0
    for lead in leads:
        if (lead.get("utm_source") or "").strip() != "metaads":
            continue
        utm_c = lead.get("utm_content") or ""
        if not utm_c:
            continue
        tc = _tokens(utm_c)
        if not tc:
            unmatched += 1
            continue
        best, best_score = None, 0.0
        for anuncio, ta in anuncio_tokens:
            s = _jaccard(tc, ta)
            if s > best_score:
                best_score, best = s, anuncio
        if best_score >= threshold and best:
            leads_per_anuncio[best] += 1
        else:
            unmatched += 1

    # 3. Anexa em creatives_list
    for c in creatives_list:
        leads_rd = leads_per_anuncio.get(c["anuncio"], 0)
        c["leads_rd"] = leads_rd
        c["cpl_rd"] = round(c["gasto"] / leads_rd, 2) if leads_rd > 0 else 0

    total_matched = sum(leads_per_anuncio.values())
    print(f"  [Match RD/Meta] leads matched: {total_matched}, sem match: {unmatched}")
    return creatives_list


# ════════════════════════════════════════════════════════════════════
# CRIPTOGRAFIA AES-256-GCM (proteção por senha)
# ════════════════════════════════════════════════════════════════════
def upload_to_supabase(file_path: str) -> bool:
    """
    Faz upload do summary.json para o bucket privado 'dashboard-data' no Supabase Storage.
    Usa a service_role key (nunca exposta no front-end).
    Retorna True em caso de sucesso.
    """
    supabase_url = config.SUPABASE_URL
    service_key  = config.SUPABASE_SERVICE_KEY

    if not supabase_url or not service_key:
        print("  AVISO: SUPABASE_URL ou SUPABASE_SERVICE_KEY não definidos — upload ignorado.")
        return False

    try:
        from supabase import create_client, Client
        sb: Client = create_client(supabase_url, service_key)

        with open(file_path, "rb") as f:
            content = f.read()

        # upsert=True substitui o arquivo existente
        sb.storage.from_("dashboard-data").upload(
            path="summary.json",
            file=content,
            file_options={"content-type": "application/json", "upsert": "true"},
        )
        print(f"  >>> summary.json enviado ao Supabase Storage com sucesso!")
        return True

    except Exception as exc:
        print(f"  ERRO ao fazer upload para o Supabase: {exc}")
        return False


def dedupe_by_id(records, source_name=""):
    """Defensive dedup: remove registros com mesmo 'id' (mantém primeira ocorrência)."""
    if not records:
        return records
    seen = set()
    result = []
    dup_count = 0
    for r in records:
        rid = (r.get("id") or "").strip()
        if not rid:
            result.append(r)  # sem id: mantém (não é duplicação detectável)
            continue
        if rid in seen:
            dup_count += 1
            continue
        seen.add(rid)
        result.append(r)
    if dup_count > 0:
        print(f"  [Dedupe] {source_name}: {dup_count} duplicatas removidas ({len(result)} únicos).")
    return result


def parse_num(v):
    if v is None or v == "":
        return 0.0
    try:
        return float(str(v).replace(",", ".").replace(" ", "").replace("R$", "").strip())
    except Exception:
        return 0.0


def _parse_dt(date_str):
    if not date_str:
        return None
    date_str = str(date_str).strip()
    for fmt in [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ]:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            pass
    if len(date_str) == 8 and date_str.isdigit():
        try:
            return datetime.strptime(date_str, "%Y%m%d")
        except ValueError:
            pass
    for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y"]:
        try:
            return datetime.strptime(date_str[:10], fmt)
        except ValueError:
            pass
    return None


def parse_date_to_month(date_str):
    dt = _parse_dt(date_str)
    return dt.strftime("%Y-%m") if dt else None


def parse_date_to_day(date_str):
    dt = _parse_dt(date_str)
    return dt.strftime("%Y-%m-%d") if dt else None


STAGE_NORMALIZE = {
    # ── Funil Ouro do Gege ────────────────────────────────────────────────────
    'contato engajado':                    'Contato Engajado',
    'modelo escolhido':                    'Modelo Escolhido',
    'interesse em visitar a loja':         'Interesse em Visitar a Loja',
    'interesse em visitar':                'Interesse em Visitar a Loja',
    'visitou e nao comprou':               'Visitou e não Comprou',
    'visitou e não comprou':               'Visitou e não Comprou',
    'visitou nao comprou':                 'Visitou e não Comprou',
    'comprou':                             'Comprou',
    'perdido':                             'Perdido',
}


def normalize_etapa(etapa):
    if not etapa:
        return 'sem etapa'
    return STAGE_NORMALIZE.get(etapa.strip().lower(), etapa.strip())


def is_repasse(campaign_name):
    if not campaign_name:
        return False
    name_lower = campaign_name.lower()
    return any(kw in name_lower for kw in REPASSE_KEYWORDS)


def aggregate_leads(leads):
    monthly = defaultdict(lambda: {"total": 0, "qualificados": 0})
    daily = defaultdict(lambda: {"total": 0, "qualificados": 0})
    by_source = defaultdict(int)
    by_canal = defaultdict(int)
    by_lifecycle = defaultdict(int)
    by_capital = defaultdict(int)
    by_prazo = defaultdict(int)
    prazo_daily = defaultdict(lambda: defaultdict(int))     # dia → prazo → count
    canal_monthly = defaultdict(lambda: defaultdict(int))
    canal_daily = defaultdict(lambda: defaultdict(int))

    for lead in leads:
        criado = lead.get("criado_em")
        mes = parse_date_to_month(criado)
        dia = parse_date_to_day(criado)
        lc = str(lead.get("lifecycle_stage") or "").lower()
        qualif = "qualif" in lc or "mql" in lc

        if mes:
            monthly[mes]["total"] += 1
            if qualif:
                monthly[mes]["qualificados"] += 1
        if dia:
            daily[dia]["total"] += 1
            if qualif:
                daily[dia]["qualificados"] += 1

        src = lead.get("utm_source") or lead.get("canal") or "direto"
        by_source[src] += 1

        canal = lead.get("canal") or "sem canal"
        by_canal[canal] += 1

        if mes:
            canal_monthly[mes][canal] += 1
        if dia:
            canal_daily[dia][canal] += 1

        lc2 = lead.get("lifecycle_stage") or "sem estágio"
        by_lifecycle[lc2] += 1

        capital = lead.get("capital_disponivel") or "não informado"
        by_capital[capital] += 1

        prazo = lead.get("prazo_abertura") or "não informado"
        by_prazo[prazo] += 1
        if dia:
            prazo_daily[dia][prazo] += 1

    pagos = sum(v for k, v in by_canal.items() if any(kw in k.lower() for kw in ["busca paga", "paid", "cpc", " paga"]))
    organicos = len(leads) - pagos

    return {
        "total": len(leads),
        "pagos": pagos,
        "organicos": organicos,
        "monthly": [{"mes": k, **v} for k, v in sorted(monthly.items())],
        "daily": [{"data": k, **v} for k, v in sorted(daily.items())],
        "by_source": [
            {"source": k, "total": v}
            for k, v in sorted(by_source.items(), key=lambda x: -x[1])[:15]
        ],
        "by_canal": [
            {"canal": k, "total": v}
            for k, v in sorted(by_canal.items(), key=lambda x: -x[1])
        ],
        "by_lifecycle": [
            {"stage": k, "total": v}
            for k, v in sorted(by_lifecycle.items(), key=lambda x: -x[1])
        ],
        "by_capital": [
            {"capital": k, "total": v}
            for k, v in sorted(by_capital.items(), key=lambda x: -x[1])
            if k != "não informado"
        ],
        "by_prazo": [
            {"prazo": k, "total": v}
            for k, v in sorted(by_prazo.items(), key=lambda x: -x[1])
            if k != "não informado"
        ],
        "prazo_daily": [
            {"data": dia, "prazo": p, "total": count}
            for dia, prazos in sorted(prazo_daily.items())
            for p, count in prazos.items()
        ],
        "canal_monthly": [
            {"mes": mes, "canal": c, "total": count}
            for mes, canals in sorted(canal_monthly.items())
            for c, count in canals.items()
        ],
        "canal_daily": [
            {"data": dia, "canal": c, "total": count}
            for dia, canals in sorted(canal_daily.items())
            for c, count in canals.items()
        ],
    }


def aggregate_crm(deals, leads=None):
    by_stage = defaultdict(lambda: {"total": 0, "valor": 0.0, "ganhos": 0})
    active_stage = defaultdict(lambda: {"total": 0, "valor": 0.0})  # apenas ativos
    by_loss_reason = defaultdict(int)
    by_loss_stage = defaultdict(int)          # perdas por etapa do funil
    by_loss_utm_source = defaultdict(int)     # perdas por utm_source
    by_loss_utm_campaign = defaultdict(int)   # perdas por utm_campaign
    by_loss_utm_content = defaultdict(int)    # perdas por utm_content (anúncio)
    losses_daily = []                         # 1 linha por deal perdido (filtrável por data)
    by_responsavel = defaultdict(lambda: {"total": 0, "ganhos": 0, "valor": 0.0})
    monthly_won = defaultdict(lambda: {"total": 0, "valor": 0.0})
    monthly_stages = defaultdict(lambda: {
        "reuniao_pdf": 0, "apresentacao_bp": 0, "perdidos": 0,
        "reuniao_agendada": 0, "reuniao_realizada": 0,
    })

    # Índice de leads por e-mail para cruzar UTMs com os deals perdidos
    lead_by_email = {}
    if leads:
        for lead in leads:
            email = (lead.get("email") or "").strip().lower()
            if email:
                lead_by_email[email] = lead

    total_won = 0
    total_lost = 0
    total_value = 0.0

    for deal in deals:
        etapa_raw = deal.get("etapa") or "sem etapa"
        etapa = normalize_etapa(etapa_raw)          # nome normalizado
        ganho_raw = str(deal.get("ganho") or "").lower()
        ganho = ganho_raw in ("true", "1", "sim", "yes")
        motivo = deal.get("motivo_perda")
        perdido = bool(motivo)
        valor = parse_num(deal.get("valor_total"))

        by_stage[etapa]["total"] += 1
        by_stage[etapa]["valor"] += valor
        if ganho:
            by_stage[etapa]["ganhos"] += 1

        # Funil de ativos: exclui ganhos, perdidos e etapas de descarte
        EXCLUIR_ATIVO = {'etapa descarte', 'sem etapa'}
        if not ganho and not perdido and etapa.lower() not in EXCLUIR_ATIVO:
            active_stage[etapa]["total"] += 1
            active_stage[etapa]["valor"] += valor

        if ganho:
            total_won += 1
            total_value += valor
            mes = parse_date_to_month(deal.get("data_fechamento") or deal.get("criado_em"))
            if mes:
                monthly_won[mes]["total"] += 1
                monthly_won[mes]["valor"] += valor

        motivo = deal.get("motivo_perda")
        if motivo:
            by_loss_reason[motivo] += 1
            by_loss_stage[etapa] += 1
            total_lost += 1

            # Cruzar UTMs via e-mail do contato
            email = (deal.get("contato_email") or "").strip().lower()
            lead = lead_by_email.get(email)
            if lead:
                src  = lead.get("utm_source")   or "não identificado"
                camp = lead.get("utm_campaign") or "não identificado"
                cont = lead.get("utm_content")  or "não identificado"
                by_loss_utm_source[src] += 1
                by_loss_utm_campaign[camp] += 1
                by_loss_utm_content[cont] += 1
            else:
                src = camp = cont = "não identificado"

            # Aggregação DIÁRIA para filtro por período no front-end.
            # Usa data_fechamento (quando a perda foi marcada) com fallback
            # para criado_em (quando o lead entrou).
            dia_perda = parse_date_to_day(deal.get("data_fechamento") or deal.get("criado_em"))
            if dia_perda:
                losses_daily.append({
                    "data": dia_perda,
                    "motivo": motivo,
                    "etapa": etapa,
                    "utm_source": src,
                    "utm_campaign": camp,
                    "utm_content": cont,
                })

        mes_criado = parse_date_to_month(deal.get("criado_em"))
        if mes_criado:
            etapa_lower = etapa.lower()
            if "reunião pdf" in etapa_lower or "reuniao pdf" in etapa_lower:
                monthly_stages[mes_criado]["reuniao_pdf"] += 1
            if "apresentação do business plan" in etapa_lower or "apresentacao do business plan" in etapa_lower:
                monthly_stages[mes_criado]["apresentacao_bp"] += 1
            if "reunião agendada" in etapa_lower or "reuniao agendada" in etapa_lower:
                monthly_stages[mes_criado]["reuniao_agendada"] += 1
            if "reunião realizada" in etapa_lower or "reuniao realizada" in etapa_lower:
                monthly_stages[mes_criado]["reuniao_realizada"] += 1
            if motivo:
                monthly_stages[mes_criado]["perdidos"] += 1

        resp = deal.get("responsavel") or "sem responsável"
        by_responsavel[resp]["total"] += 1
        if ganho:
            by_responsavel[resp]["ganhos"] += 1
            by_responsavel[resp]["valor"] += valor

    total_open = len(deals) - total_won - total_lost

    funnel = sorted(by_stage.items(), key=lambda x: -x[1]["total"])

    # Funil de ativos na ordem correta do pipeline
    PIPELINE_ORDER = [
        'contato engajado',
        'modelo escolhido',
        'interesse em visitar a loja',
        'visitou e não comprou',
        'comprou',
        'perdido',
    ]
    def stage_sort_key(name):
        nl = name.lower()
        for i, s in enumerate(PIPELINE_ORDER):
            if nl == s or nl in s or s in nl:
                return i
        return 999

    active_funnel_sorted = sorted(active_stage.items(), key=lambda x: stage_sort_key(x[0]))

    # ── Lista minimal de deals para "Funil de Conversão do Período" no frontend ──
    # Permite filtro por data e cálculo de passed-through por etapa.
    # Mantém leve: só campos necessários (etapa normalizada + criado_em).
    deals_minimal = []
    for deal in deals:
        etapa = normalize_etapa(deal.get("etapa") or "sem etapa")
        criado = parse_date_to_day(deal.get("criado_em"))
        if criado:
            deals_minimal.append({"etapa": etapa, "criado_em": criado})

    # ── Perdas: separação pago vs orgânico ───────────────────────────────────
    # Considera-se "pago" qualquer lead cuja utm_source seja googlecpc ou metaads.
    # Os demais (direto, organico, e-mail, "não identificado", etc.) entram em
    # orgânico para que a soma feche com total_lost.
    PAID_SRCS = {"googlecpc", "metaads"}
    total_lost_pago     = sum(v for k, v in by_loss_utm_source.items() if k in PAID_SRCS)
    total_lost_organico = total_lost - total_lost_pago

    return {
        "total_deals": len(deals),
        "total_won": total_won,
        "total_lost": total_lost,
        "total_lost_pago": total_lost_pago,
        "total_lost_organico": total_lost_organico,
        "total_open": total_open,
        "total_active": sum(v["total"] for v in active_stage.values()),
        "total_value_won": round(total_value, 2),
        "taxa_fechamento": round(total_won / len(deals) * 100, 1) if deals else 0,
        "deals_minimal": deals_minimal,   # para Funil de Conversão do Período
        "funnel": [
            {"etapa": k, "total": v["total"], "valor": round(v["valor"], 2), "ganhos": v["ganhos"]}
            for k, v in funnel
        ],
        "active_funnel": [
            {"etapa": k, "total": v["total"], "valor": round(v["valor"], 2)}
            for k, v in active_funnel_sorted
        ],
        "losses": [
            {"motivo": k, "total": v}
            for k, v in sorted(by_loss_reason.items(), key=lambda x: -x[1])
        ],
        "losses_by_stage": [
            {"etapa": k, "total": v}
            for k, v in sorted(by_loss_stage.items(), key=lambda x: -x[1])
        ],
        "losses_by_utm_source": [
            {"utm": k, "total": v}
            for k, v in sorted(by_loss_utm_source.items(), key=lambda x: -x[1])
        ],
        "losses_by_utm_campaign": [
            {"utm": k, "total": v}
            for k, v in sorted(by_loss_utm_campaign.items(), key=lambda x: -x[1])
        ],
        "losses_by_utm_content": [
            {"utm": k, "total": v}
            for k, v in sorted(by_loss_utm_content.items(), key=lambda x: -x[1])
        ],
        # 1 linha por deal perdido, com data + dimensões — usado pelos filtros
        # da página "Gargalos & Perdas" no front-end.
        "losses_daily": losses_daily,
        "by_responsavel": [
            {"responsavel": k, "total": v["total"], "ganhos": v["ganhos"], "valor": round(v["valor"], 2)}
            for k, v in sorted(by_responsavel.items(), key=lambda x: -x[1]["total"])
        ],
        "monthly_won": [
            {"mes": k, "total": v["total"], "valor": round(v["valor"], 2)}
            for k, v in sorted(monthly_won.items())
        ],
        "monthly_stages": [
            {
                "mes": k,
                "reuniao_pdf": v["reuniao_pdf"],
                "apresentacao_bp": v["apresentacao_bp"],
                "perdidos": v["perdidos"],
                "reuniao_agendada": v["reuniao_agendada"],
                "reuniao_realizada": v["reuniao_realizada"],
            }
            for k, v in sorted(monthly_stages.items())
        ],
    }


def aggregate_meta_ads(rows, leads_daily_map=None):
    monthly = defaultdict(lambda: {
        "gasto": 0.0, "leads": 0.0, "impressoes": 0.0, "cliques": 0.0,
        "expansao_gasto": 0.0, "repasse_gasto": 0.0,
        "expansao_leads": 0.0, "repasse_leads": 0.0,
    })
    daily = defaultdict(lambda: {"gasto": 0.0, "leads": 0.0, "impressoes": 0.0, "cliques": 0.0})
    campaigns = defaultdict(lambda: {
        "gasto": 0.0, "leads": 0.0, "impressoes": 0.0, "cliques": 0.0, "tipo": "Expansão"
    })
    camp_monthly = defaultdict(lambda: defaultdict(lambda: {
        "gasto": 0.0, "leads": 0.0, "impressoes": 0.0, "cliques": 0.0,
    }))
    creatives = defaultdict(lambda: {
        "gasto": 0.0, "leads": 0.0, "impressoes": 0.0, "cliques": 0.0,
        "thumbnail": "", "campanha": "", "conjunto": "", "permalink": "",
    })
    # Diário por criativo — permite filtro de data no front-end (Top 5 do período)
    creatives_daily = defaultdict(lambda: defaultdict(lambda: {
        "gasto": 0.0, "leads": 0.0, "impressoes": 0.0, "cliques": 0.0,
    }))

    for row in rows:
        mes = parse_date_to_month(row.get("data"))
        dia = parse_date_to_day(row.get("data"))
        if not mes:
            continue

        gasto = parse_num(row.get("gasto"))
        leads = parse_num(row.get("leads"))
        impressoes = parse_num(row.get("impressoes"))
        cliques = parse_num(row.get("cliques_link"))
        campanha = row.get("campanha") or ""
        tipo = "Repasse" if is_repasse(campanha) else "Expansão"

        monthly[mes]["gasto"] += gasto
        monthly[mes]["leads"] += leads
        monthly[mes]["impressoes"] += impressoes
        monthly[mes]["cliques"] += cliques

        if dia:
            daily[dia]["gasto"] += gasto
            daily[dia]["leads"] += leads
            daily[dia]["impressoes"] += impressoes
            daily[dia]["cliques"] += cliques

        if tipo == "Repasse":
            monthly[mes]["repasse_gasto"] += gasto
            monthly[mes]["repasse_leads"] += leads
        else:
            monthly[mes]["expansao_gasto"] += gasto
            monthly[mes]["expansao_leads"] += leads

        if campanha:
            campaigns[campanha]["gasto"] += gasto
            campaigns[campanha]["leads"] += leads
            campaigns[campanha]["impressoes"] += impressoes
            campaigns[campanha]["cliques"] += cliques
            campaigns[campanha]["tipo"] = tipo

            camp_monthly[campanha][mes]["gasto"] += gasto
            camp_monthly[campanha][mes]["leads"] += leads
            camp_monthly[campanha][mes]["impressoes"] += impressoes
            camp_monthly[campanha][mes]["cliques"] += cliques

        anuncio = row.get("anuncio") or ""
        if anuncio:
            creatives[anuncio]["gasto"] += gasto
            creatives[anuncio]["leads"] += leads
            creatives[anuncio]["impressoes"] += impressoes
            creatives[anuncio]["cliques"] += cliques
            if row.get("thumbnail"):
                creatives[anuncio]["thumbnail"] = row.get("thumbnail")
            if row.get("permalink"):
                creatives[anuncio]["permalink"] = row.get("permalink")
            if row.get("conjunto"):
                creatives[anuncio]["conjunto"] = row.get("conjunto")
            if campanha:  # sempre atualiza para o nome mais recente da campanha
                creatives[anuncio]["campanha"] = campanha

            # Diário por criativo (para filtro de data no Top 5 da página Criativos)
            if dia:
                creatives_daily[anuncio][dia]["gasto"] += gasto
                creatives_daily[anuncio][dia]["leads"] += leads
                creatives_daily[anuncio][dia]["impressoes"] += impressoes
                creatives_daily[anuncio][dia]["cliques"] += cliques

    monthly_list = []
    for mes, d in sorted(monthly.items()):
        cpl = round(d["gasto"] / d["leads"], 2) if d["leads"] > 0 else 0
        ctr = round(d["cliques"] / d["impressoes"] * 100, 2) if d["impressoes"] > 0 else 0
        monthly_list.append({
            "mes": mes,
            "gasto": round(d["gasto"], 2),
            "leads": int(round(d["leads"])),
            "cpl": cpl,
            "impressoes": int(round(d["impressoes"])),
            "cliques": int(round(d["cliques"])),
            "ctr": ctr,
            "expansao_gasto": round(d["expansao_gasto"], 2),
            "repasse_gasto": round(d["repasse_gasto"], 2),
            "expansao_leads": int(round(d["expansao_leads"])),
            "repasse_leads": int(round(d["repasse_leads"])),
        })

    campaigns_list = []
    for nome, d in sorted(campaigns.items(), key=lambda x: -x[1]["gasto"])[:30]:
        cpl = round(d["gasto"] / d["leads"], 2) if d["leads"] > 0 else 0
        ctr = round(d["cliques"] / d["impressoes"] * 100, 2) if d["impressoes"] > 0 else 0
        campaigns_list.append({
            "campanha": nome, "tipo": d["tipo"],
            "gasto": round(d["gasto"], 2), "leads": int(round(d["leads"])),
            "cpl": cpl, "impressoes": int(round(d["impressoes"])),
            "cliques": int(round(d["cliques"])), "ctr": ctr,
        })

    creatives_list = []
    for nome, d in sorted(creatives.items(), key=lambda x: -x[1]["gasto"])[:50]:
        cpl = round(d["gasto"] / d["leads"], 2) if d["leads"] > 0 else 0
        ctr = round(d["cliques"] / d["impressoes"] * 100, 2) if d["impressoes"] > 0 else 0
        creatives_list.append({
            "anuncio": nome, "campanha": d["campanha"],
            "conjunto": d.get("conjunto", ""),
            "thumbnail": d["thumbnail"],
            "permalink": d.get("permalink", ""),
            "gasto": round(d["gasto"], 2), "leads": int(round(d["leads"])),
            "cpl": cpl, "impressoes": int(round(d["impressoes"])),
            "cliques": int(round(d["cliques"])), "ctr": ctr,
        })

    campaign_monthly_list = []
    for camp, months in sorted(camp_monthly.items()):
        tipo = "Repasse" if is_repasse(camp) else "Expansão"
        for mes, d in sorted(months.items()):
            campaign_monthly_list.append({
                "campanha": camp, "mes": mes, "tipo": tipo,
                "gasto": round(d["gasto"], 2), "leads": int(round(d["leads"])),
                "impressoes": int(round(d["impressoes"])), "cliques": int(round(d["cliques"])),
            })

    total_gasto = sum(d["gasto"] for d in monthly.values())
    total_leads = sum(d["leads"] for d in monthly.values())

    daily_list = [
        {"data": d, "gasto": round(v["gasto"], 2), "leads": int(round(v["leads"])),
         "impressoes": int(round(v["impressoes"])), "cliques": int(round(v["cliques"]))}
        for d, v in sorted(daily.items())
    ]

    # Lista flat (anuncio x data). Campo `leads` = LEADS DO CRM (via matching
    # utm_content ↔ anuncio), NÃO os "leads" da plataforma Meta (que são imprecisos).
    # Sem thumbnail/permalink (otimização de tamanho — front faz lookup no `creatives`).
    creatives_daily_list = []
    leads_daily_map = leads_daily_map or {}
    for nome, dias in creatives_daily.items():
        ad_leads_map = leads_daily_map.get(nome, {})
        for dia, v in sorted(dias.items()):
            leads_crm = ad_leads_map.get(dia, 0)
            if v["gasto"] == 0 and leads_crm == 0:
                continue
            creatives_daily_list.append({
                "data":    dia,
                "anuncio": nome,
                "gasto":   round(v["gasto"], 2),
                "leads":   leads_crm,   # <-- CRM, não plataforma!
            })

    return {
        "total_gasto": round(total_gasto, 2),
        "total_leads": int(round(total_leads)),
        "cpl_geral": round(total_gasto / total_leads, 2) if total_leads > 0 else 0,
        "monthly": monthly_list,
        "daily": daily_list,
        "campaigns": campaigns_list,
        "campaign_monthly": campaign_monthly_list,
        "campaign_names": sorted(camp_monthly.keys()),
        "creatives": creatives_list,
        "creatives_daily": creatives_daily_list,
    }


def aggregate_google_ads(rows):
    monthly = defaultdict(lambda: {
        "gasto": 0.0, "conversoes": 0.0, "impressoes": 0.0, "cliques": 0.0
    })
    daily = defaultdict(lambda: {"gasto": 0.0, "conversoes": 0.0, "impressoes": 0.0, "cliques": 0.0})
    campaigns = defaultdict(lambda: {
        "gasto": 0.0, "conversoes": 0.0, "impressoes": 0.0, "cliques": 0.0
    })
    camp_monthly = defaultdict(lambda: defaultdict(lambda: {
        "gasto": 0.0, "conversoes": 0.0, "impressoes": 0.0, "cliques": 0.0,
    }))

    for row in rows:
        mes = parse_date_to_month(row.get("data"))
        dia = parse_date_to_day(row.get("data"))
        if not mes:
            continue

        gasto = parse_num(row.get("gasto"))
        conversoes = parse_num(row.get("conversoes"))
        impressoes = parse_num(row.get("impressoes"))
        cliques = parse_num(row.get("cliques"))
        campanha = row.get("campanha") or ""

        monthly[mes]["gasto"] += gasto
        monthly[mes]["conversoes"] += conversoes
        monthly[mes]["impressoes"] += impressoes
        monthly[mes]["cliques"] += cliques

        if dia:
            daily[dia]["gasto"] += gasto
            daily[dia]["conversoes"] += conversoes
            daily[dia]["impressoes"] += impressoes
            daily[dia]["cliques"] += cliques

        if campanha:
            campaigns[campanha]["gasto"] += gasto
            campaigns[campanha]["conversoes"] += conversoes
            campaigns[campanha]["impressoes"] += impressoes
            campaigns[campanha]["cliques"] += cliques

            camp_monthly[campanha][mes]["gasto"] += gasto
            camp_monthly[campanha][mes]["conversoes"] += conversoes
            camp_monthly[campanha][mes]["impressoes"] += impressoes
            camp_monthly[campanha][mes]["cliques"] += cliques

    monthly_list = []
    for mes, d in sorted(monthly.items()):
        cpl = round(d["gasto"] / d["conversoes"], 2) if d["conversoes"] > 0 else 0
        ctr = round(d["cliques"] / d["impressoes"] * 100, 2) if d["impressoes"] > 0 else 0
        monthly_list.append({
            "mes": mes,
            "gasto": round(d["gasto"], 2),
            "conversoes": int(round(d["conversoes"])),
            "cpl": cpl,
            "impressoes": int(round(d["impressoes"])),
            "cliques": int(round(d["cliques"])),
            "ctr": ctr,
        })

    campaigns_list = []
    for nome, d in sorted(campaigns.items(), key=lambda x: -x[1]["gasto"])[:30]:
        cpl = round(d["gasto"] / d["conversoes"], 2) if d["conversoes"] > 0 else 0
        ctr = round(d["cliques"] / d["impressoes"] * 100, 2) if d["impressoes"] > 0 else 0
        campaigns_list.append({
            "campanha": nome,
            "gasto": round(d["gasto"], 2), "conversoes": int(round(d["conversoes"])),
            "cpl": cpl, "impressoes": int(round(d["impressoes"])),
            "cliques": int(round(d["cliques"])), "ctr": ctr,
        })

    campaign_monthly_list = []
    for camp, months in sorted(camp_monthly.items()):
        for mes, d in sorted(months.items()):
            campaign_monthly_list.append({
                "campanha": camp, "mes": mes,
                "gasto": round(d["gasto"], 2), "conversoes": int(round(d["conversoes"])),
                "impressoes": int(round(d["impressoes"])), "cliques": int(round(d["cliques"])),
            })

    total_gasto = sum(d["gasto"] for d in monthly.values())
    total_conversoes = sum(d["conversoes"] for d in monthly.values())

    daily_list = [
        {"data": d, "gasto": round(v["gasto"], 2), "conversoes": int(round(v["conversoes"])),
         "impressoes": int(round(v["impressoes"])), "cliques": int(round(v["cliques"]))}
        for d, v in sorted(daily.items())
    ]

    return {
        "total_gasto": round(total_gasto, 2),
        "total_conversoes": int(round(total_conversoes)),
        "cpl_geral": round(total_gasto / total_conversoes, 2) if total_conversoes > 0 else 0,
        "monthly": monthly_list,
        "daily": daily_list,
        "campaigns": campaigns_list,
        "campaign_monthly": campaign_monthly_list,
        "campaign_names": sorted(camp_monthly.keys()),
    }


def aggregate_ga4(rows):
    monthly = defaultdict(lambda: {
        "sessions": 0, "users": 0, "new_users": 0, "conversions": 0,
        "bounce_weighted": 0.0, "duration_weighted": 0.0,
    })
    channels = defaultdict(lambda: {"sessions": 0, "users": 0, "conversions": 0})
    devices = defaultdict(int)
    pages = defaultdict(int)
    sources = defaultdict(int)

    for row in rows:
        mes = parse_date_to_month(row.get("date"))
        if not mes:
            continue

        sessions = int(parse_num(row.get("sessions")))
        users = int(parse_num(row.get("totalUsers")))
        new_users = int(parse_num(row.get("newUsers")))
        conversions = int(parse_num(row.get("conversions")))
        bounce = parse_num(row.get("bounceRate"))
        duration = parse_num(row.get("averageSessionDuration"))

        monthly[mes]["sessions"] += sessions
        monthly[mes]["users"] += users
        monthly[mes]["new_users"] += new_users
        monthly[mes]["conversions"] += conversions
        monthly[mes]["bounce_weighted"] += bounce * sessions
        monthly[mes]["duration_weighted"] += duration * sessions

        channel = row.get("sessionDefaultChannelGroup") or "outros"
        channels[channel]["sessions"] += sessions
        channels[channel]["users"] += users
        channels[channel]["conversions"] += conversions

        device = row.get("deviceCategory") or "outros"
        devices[device] += sessions

        page = row.get("pagePath") or "/"
        pages[page] += int(parse_num(row.get("screenPageViews")))

        source = row.get("sessionSource") or "direto"
        sources[source] += sessions

    monthly_list = []
    for mes, d in sorted(monthly.items()):
        s = d["sessions"]
        avg_bounce = round(d["bounce_weighted"] / s * 100, 1) if s > 0 else 0
        avg_duration = round(d["duration_weighted"] / s, 1) if s > 0 else 0
        monthly_list.append({
            "mes": mes,
            "sessions": s,
            "users": d["users"],
            "new_users": d["new_users"],
            "conversions": d["conversions"],
            "bounce_rate": avg_bounce,
            "avg_duration": avg_duration,
        })

    return {
        "total_sessions": sum(d["sessions"] for d in monthly.values()),
        "total_users": sum(d["users"] for d in monthly.values()),
        "monthly": monthly_list,
        "channels": [
            {"channel": k, "sessions": v["sessions"], "users": v["users"], "conversions": v["conversions"]}
            for k, v in sorted(channels.items(), key=lambda x: -x[1]["sessions"])
        ],
        "devices": [
            {"device": k, "sessions": v}
            for k, v in sorted(devices.items(), key=lambda x: -x[1])
        ],
        "top_pages": [
            {"page": k, "views": v}
            for k, v in sorted(pages.items(), key=lambda x: -x[1])[:20]
        ],
        "sources": [
            {"source": k, "sessions": v}
            for k, v in sorted(sources.items(), key=lambda x: -x[1])[:15]
        ],
    }


def aggregate_utm(leads):
    sources = defaultdict(int)
    mediums = defaultdict(int)
    campaigns_utm = defaultdict(int)
    contents_utm = defaultdict(int)                         # utm_content → total leads
    campaigns_src = defaultdict(lambda: defaultdict(int))   # campaign -> source -> count
    sources_monthly = defaultdict(lambda: defaultdict(int))
    mediums_monthly = defaultdict(lambda: defaultdict(int))
    campaigns_monthly = defaultdict(lambda: defaultdict(int))
    sources_daily = defaultdict(lambda: defaultdict(int))
    mediums_daily = defaultdict(lambda: defaultdict(int))
    campaigns_daily = defaultdict(lambda: defaultdict(int))
    campaigns_src_daily = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

    for lead in leads:
        src = lead.get("utm_source") or "direto"
        med = lead.get("utm_medium") or "nenhum"
        camp = lead.get("utm_campaign")
        cont = lead.get("utm_content")
        if cont:
            contents_utm[cont] += 1

        sources[src] += 1
        mediums[med] += 1
        if camp:
            campaigns_utm[camp] += 1
            campaigns_src[camp][src] += 1

        mes = parse_date_to_month(lead.get("criado_em"))
        dia = parse_date_to_day(lead.get("criado_em"))

        if mes:
            sources_monthly[mes][src] += 1
            mediums_monthly[mes][med] += 1
            if camp:
                campaigns_monthly[mes][camp] += 1
        if dia:
            sources_daily[dia][src] += 1
            mediums_daily[dia][med] += 1
            if camp:
                campaigns_daily[dia][camp] += 1
                campaigns_src_daily[dia][camp][src] += 1

    def dominant_source(src_dict):
        return max(src_dict, key=src_dict.get) if src_dict else "direto"

    return {
        "sources": [
            {"source": k, "total": v}
            for k, v in sorted(sources.items(), key=lambda x: -x[1])
        ],
        "mediums": [
            {"medium": k, "total": v}
            for k, v in sorted(mediums.items(), key=lambda x: -x[1])
        ],
        "campaigns": [
            {"campaign": k, "total": v, "source": dominant_source(campaigns_src[k])}
            for k, v in sorted(campaigns_utm.items(), key=lambda x: -x[1])[:20]
        ],
        "contents": [
            {"content": k, "total": v}
            for k, v in sorted(contents_utm.items(), key=lambda x: -x[1])[:30]
        ],
        "sources_monthly": [
            {"mes": mes, "source": src, "total": count}
            for mes, srcs in sorted(sources_monthly.items())
            for src, count in srcs.items()
        ],
        "mediums_monthly": [
            {"mes": mes, "medium": m, "total": count}
            for mes, meds in sorted(mediums_monthly.items())
            for m, count in meds.items()
        ],
        "campaigns_monthly": [
            {"mes": mes, "campaign": c, "total": count}
            for mes, camps in sorted(campaigns_monthly.items())
            for c, count in camps.items()
        ],
        "sources_daily": [
            {"data": dia, "source": src, "total": count}
            for dia, srcs in sorted(sources_daily.items())
            for src, count in srcs.items()
        ],
        "mediums_daily": [
            {"data": dia, "medium": m, "total": count}
            for dia, meds in sorted(mediums_daily.items())
            for m, count in meds.items()
        ],
        "campaigns_daily": [
            {
                "data": dia, "campaign": c, "total": count,
                "source": dominant_source(campaigns_src_daily[dia][c]),
            }
            for dia, camps in sorted(campaigns_daily.items())
            for c, count in camps.items()
        ],
    }



def aggregate_google_ads_creatives(rows, leads_daily_map=None):
    """
    Agrega criativos do Google Ads (sheet 'google_ads_creatives').

    Cada linha de entrada = (data, anúncio). Para o dashboard:
      - daily: lista flat (data x anuncio) com leads do CRM (matching utm_content)
      - creatives: agregado por anúncio com leads_crm/cpl_crm

    Importante: As "conversões" da planilha Google são da plataforma — imprecisas.
    `leads_crm` é a fonte de verdade (vem do RD Station via Jaccard match).
    """
    if not rows:
        return {"daily": [], "creatives": []}

    daily_list = []
    by_ad = defaultdict(lambda: {
        "gasto": 0.0, "conversoes_plataforma": 0.0,
        "campanha": "", "grupo": "", "tipo": "",
        "url_final": "", "cta": "", "qualidade": "",
        "headlines": "", "descriptions": "",
        "primeira_data": "", "ultima_data": "",
    })

    leads_daily_map = leads_daily_map or {}

    for r in rows:
        data = (r.get("data") or "").strip()
        nome = (r.get("anuncio") or "").strip()
        if not data or not nome:
            continue
        gasto = parse_num(r.get("custo"))
        conv  = parse_num(r.get("conversoes"))
        # Leads do CRM neste dia para este anúncio (matching prévio)
        leads_crm = leads_daily_map.get(nome, {}).get(data, 0)

        # Linha flat (data, anuncio) — para Top 5 com filtro de data
        if gasto > 0 or leads_crm > 0:
            daily_list.append({
                "data":     data,
                "anuncio":  nome,
                "campanha": r.get("campanha") or "",
                "tipo":     r.get("tipo") or "",
                "gasto":    round(gasto, 2),
                "leads":    leads_crm,   # <-- CRM, fonte de verdade
            })

        # Agregado por anuncio
        d = by_ad[nome]
        d["gasto"]                 += gasto
        d["conversoes_plataforma"] += conv
        for f in ("campanha", "grupo", "tipo", "url_final", "cta", "qualidade",
                  "headlines", "descriptions"):
            v = (r.get(f) or "").strip()
            if v:
                d[f] = v
        if not d["primeira_data"] or data < d["primeira_data"]:
            d["primeira_data"] = data
        if data > d["ultima_data"]:
            d["ultima_data"] = data

    # Soma de leads_crm por anúncio (todos os dias)
    leads_crm_total = {nome: sum(dias.values()) for nome, dias in leads_daily_map.items()}

    creatives_list = []
    for nome, d in sorted(by_ad.items(), key=lambda x: -x[1]["gasto"]):
        leads_crm = leads_crm_total.get(nome, 0)
        cpl_crm   = round(d["gasto"] / leads_crm, 2) if leads_crm > 0 else 0
        creatives_list.append({
            "anuncio":      nome,
            "campanha":     d["campanha"],
            "grupo":        d["grupo"],
            "tipo":         d["tipo"],
            "url_final":    d["url_final"],
            "cta":          d["cta"],
            "qualidade":    d["qualidade"],
            "headlines":    d["headlines"],
            "descriptions": d["descriptions"],
            "gasto":        round(d["gasto"], 2),
            "leads_crm":    leads_crm,                                    # <-- VERDADE
            "cpl_crm":      cpl_crm,                                      # <-- VERDADE
            "conversoes_plataforma": int(round(d["conversoes_plataforma"])),  # ref
            "primeira_data": d["primeira_data"],
            "ultima_data":   d["ultima_data"],
        })

    return {
        "daily":     daily_list,
        "creatives": creatives_list,
    }


def build_relatorio(leads, crm, meta_agg, google_agg, utm_agg, meta_rows=None):
    """
    Pré-calcula as métricas do Relatório Resumido para o mês atual.
    Retorna um dict pronto para ser renderizado no dashboard sem lógica extra no front.
    """
    # Tradução manual dos meses para evitar dependência do locale pt_BR do sistema
    MESES_PT = ["", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
    def _label_mes(dt):
        return f"{MESES_PT[dt.month]}/{dt.year}"

    agora = datetime.now()
    mes_atual = agora.strftime("%Y-%m")
    mes_label = _label_mes(agora)

    # ── Leads do mês ──────────────────────────────────────────────────────────
    # Pagos vêm das PLATAFORMAS (Meta "Leads do Website" + Google "Conversões"),
    # pois os leads do CRM (SprintHub) não trazem UTM/origem.
    google_leads_mes = round(sum(r.get("conversoes", 0) for r in google_agg.get("daily", [])
                                 if (r.get("data") or "").startswith(mes_atual)))
    meta_leads_mes   = round(sum(r.get("leads", 0) for r in meta_agg.get("daily", [])
                                 if (r.get("data") or "").startswith(mes_atual)))
    pago_leads_mes   = google_leads_mes + meta_leads_mes
    # Total de leads do mês = leads do CRM (SprintHub) criados no mês.
    total_leads_mes  = sum(1 for l in leads
                           if parse_date_to_month(l.get("criado_em")) == mes_atual)
    organico_leads_mes = max(0, total_leads_mes - pago_leads_mes)
    direto_leads_mes   = organico_leads_mes

    # ── Gasto e CPL do mês ───────────────────────────────────────────────────
    meta_gasto_mes   = sum(r.get("gasto", 0) for r in meta_agg.get("daily", [])
                           if (r.get("data") or "").startswith(mes_atual))
    google_gasto_mes = sum(r.get("gasto", 0) for r in google_agg.get("daily", [])
                           if (r.get("data") or "").startswith(mes_atual))
    total_gasto_mes  = meta_gasto_mes + google_gasto_mes

    cpl_meta   = round(meta_gasto_mes / meta_leads_mes, 2)   if meta_leads_mes   else None
    cpl_google = round(google_gasto_mes / google_leads_mes, 2) if google_leads_mes else None
    cpl_pago   = round(total_gasto_mes / pago_leads_mes, 2)   if pago_leads_mes   else None

    # ── Funil CRM ─────────────────────────────────────────────────────────────
    total_deals    = crm.get("total_deals", 0)
    total_won      = crm.get("total_won", 0)
    total_lost     = crm.get("total_lost", 0)
    total_active   = crm.get("total_active", 0)

    reuniao_pdf_total  = next((e["total"] for e in crm.get("funnel", [])
                               if "reuni" in e["etapa"].lower() and "pdf" in e["etapa"].lower()), 0)
    reuniao_pdf_ativo  = next((e["total"] for e in crm.get("active_funnel", [])
                               if "reuni" in e["etapa"].lower() and "pdf" in e["etapa"].lower()), 0)

    taxa_reuniao_total = round(reuniao_pdf_total / total_deals * 100, 1) if total_deals else 0
    taxa_reuniao_ativo = round(reuniao_pdf_ativo / total_active * 100, 1) if total_active else 0

    # Reunião PDF do MÊS ATUAL (deals criados no mês cuja etapa atual é Reunião PDF)
    reuniao_pdf_mes = next((r.get("reuniao_pdf", 0) for r in crm.get("monthly_stages", [])
                            if r["mes"] == mes_atual), 0)
    # Taxa do mês: % de leads do mês que chegaram em Reunião PDF
    taxa_reuniao_mes = round(reuniao_pdf_mes / total_leads_mes * 100, 1) if total_leads_mes else None

    # ── Perdas do mês ─────────────────────────────────────────────────────────
    perdas_mes = next((r["perdidos"] for r in crm.get("monthly_stages", [])
                       if r["mes"] == mes_atual), 0)

    top_motivos = [
        {"motivo": r["motivo"], "total": r["total"]}
        for r in crm.get("losses", [])[:5]
    ]

    # ── Leads históricos por canal ─────────────────────────────────────────────
    src_map = {r["source"]: r["total"] for r in utm_agg.get("sources", [])}

    # ── Funil ativo detalhado ──────────────────────────────────────────────────
    funil_ativo = [
        {"etapa": e["etapa"], "total": e["total"]}
        for e in crm.get("active_funnel", [])
    ]

    # ── Comparativo mês anterior ──────────────────────────────────────────────
    from datetime import datetime as _dt, timedelta
    primeiro_dia_mes = _dt.now().replace(day=1)
    mes_ant_dt  = primeiro_dia_mes - timedelta(days=1)
    mes_anterior = mes_ant_dt.strftime("%Y-%m")
    mes_ant_label = _label_mes(mes_ant_dt)

    # Mês anterior — mesma lógica: pagos das plataformas, total do CRM.
    ant_google = round(sum(r.get("conversoes", 0) for r in google_agg.get("daily", [])
                           if (r.get("data") or "").startswith(mes_anterior)))
    ant_meta   = round(sum(r.get("leads", 0) for r in meta_agg.get("daily", [])
                           if (r.get("data") or "").startswith(mes_anterior)))
    ant_pago   = ant_google + ant_meta
    ant_total  = sum(1 for l in leads
                     if parse_date_to_month(l.get("criado_em")) == mes_anterior)

    ant_meta_gasto   = sum(r.get("gasto", 0) for r in meta_agg.get("daily", [])
                           if (r.get("data") or "").startswith(mes_anterior))
    ant_google_gasto = sum(r.get("gasto", 0) for r in google_agg.get("daily", [])
                           if (r.get("data") or "").startswith(mes_anterior))
    ant_cpl_meta   = round(ant_meta_gasto / ant_meta, 2)     if ant_meta   else None
    ant_cpl_google = round(ant_google_gasto / ant_google, 2) if ant_google else None
    ant_cpl_pago   = round((ant_meta_gasto + ant_google_gasto) / ant_pago, 2) if ant_pago else None

    ant_reuniao = next((r.get("reuniao_pdf", 0) for r in crm.get("monthly_stages", [])
                        if r["mes"] == mes_anterior), 0)

    def variacao(atual, anterior):
        # Se qualquer um dos lados for None ou 0, não há base de comparação confiável
        if atual is None or anterior is None or not anterior:
            return None
        return round((atual - anterior) / anterior * 100, 1)

    comparativo = {
        "mes_anterior": mes_anterior,
        "mes_ant_label": mes_ant_label,
        "leads_total":  {"atual": total_leads_mes,   "anterior": ant_total,   "delta": variacao(total_leads_mes,   ant_total)},
        "leads_pago":   {"atual": pago_leads_mes,    "anterior": ant_pago,    "delta": variacao(pago_leads_mes,    ant_pago)},
        "leads_google": {"atual": google_leads_mes,  "anterior": ant_google,  "delta": variacao(google_leads_mes,  ant_google)},
        "leads_meta":   {"atual": meta_leads_mes,    "anterior": ant_meta,    "delta": variacao(meta_leads_mes,    ant_meta)},
        "cpl_meta":     {"atual": cpl_meta,          "anterior": ant_cpl_meta,   "delta": variacao(cpl_meta,   ant_cpl_meta)},
        "cpl_google":   {"atual": cpl_google,        "anterior": ant_cpl_google, "delta": variacao(cpl_google, ant_cpl_google)},
        "cpl_pago":     {"atual": cpl_pago,          "anterior": ant_cpl_pago,   "delta": variacao(cpl_pago,   ant_cpl_pago)},
        # Reunião PDF: comparação MENSAL (deals criados no mês cuja etapa virou Reunião PDF)
        "reuniao_pdf":  {"atual": reuniao_pdf_mes,   "anterior": ant_reuniao,    "delta": variacao(reuniao_pdf_mes, ant_reuniao)},
    }

    # ── Semáforo de saúde ─────────────────────────────────────────────────────
    def semaforo_cpl_meta(v):
        if v is None: return "gray"
        return "green" if v < 200 else ("yellow" if v <= 350 else "red")
    def semaforo_cpl_google(v):
        if v is None: return "gray"
        return "green" if v < 150 else ("yellow" if v <= 250 else "red")
    def semaforo_cpl_pago(v):
        if v is None: return "gray"
        return "green" if v < 200 else ("yellow" if v <= 300 else "red")
    def semaforo_taxa_reuniao(v):
        if v is None: return "gray"
        return "green" if v >= 12 else ("yellow" if v >= 8 else "red")

    semaforo = [
        {"label": "CPL Meta Ads",    "valor": cpl_meta,    "status": semaforo_cpl_meta(cpl_meta),
         "ref": "< R$200 🟢  R$200-350 🟡  > R$350 🔴"},
        {"label": "CPL Google Ads",  "valor": cpl_google,  "status": semaforo_cpl_google(cpl_google),
         "ref": "< R$150 🟢  R$150-250 🟡  > R$250 🔴"},
        {"label": "CPL Médio Pago",  "valor": cpl_pago,    "status": semaforo_cpl_pago(cpl_pago),
         "ref": "< R$200 🟢  R$200-300 🟡  > R$300 🔴"},
        # Taxa do mês: % de leads do mês atual que chegaram em Reunião PDF
        {"label": "Taxa Reunião PDF (mês)","valor": taxa_reuniao_mes, "status": semaforo_taxa_reuniao(taxa_reuniao_mes),
         "ref": "> 12% 🟢  8-12% 🟡  < 8% 🔴", "tipo": "pct"},
    ]

    # ── Top criativos do mês ──────────────────────────────────────────────────
    top_criativos = []
    if meta_rows:
        criat_mes = defaultdict(lambda: {
            "gasto": 0.0, "leads": 0.0, "thumbnail": "", "permalink": "", "campanha": "", "conjunto": ""
        })
        for row in meta_rows:
            # Usa parse_date_to_month (mesma lógica do resto do script) para
            # tolerar formatos de data diferentes na planilha (ex.: dd/mm/yyyy)
            if parse_date_to_month(row.get("data")) != mes_atual:
                continue
            nome = (row.get("anuncio") or "").strip()
            if not nome:
                continue
            criat_mes[nome]["gasto"]  += parse_num(row.get("gasto"))
            criat_mes[nome]["leads"]  += parse_num(row.get("leads"))
            if row.get("thumbnail") and not criat_mes[nome]["thumbnail"]:
                criat_mes[nome]["thumbnail"] = row.get("thumbnail")
            if row.get("permalink") and not criat_mes[nome]["permalink"]:
                criat_mes[nome]["permalink"] = row.get("permalink")
            if row.get("campanha") and not criat_mes[nome]["campanha"]:
                criat_mes[nome]["campanha"] = row.get("campanha")
            if row.get("conjunto") and not criat_mes[nome]["conjunto"]:
                criat_mes[nome]["conjunto"] = row.get("conjunto")

        for nome, d in sorted(criat_mes.items(), key=lambda x: -x[1]["leads"])[:5]:
            cpl_c = round(d["gasto"] / d["leads"], 2) if d["leads"] > 0 else None
            top_criativos.append({
                "anuncio":   nome,
                "campanha":  d["campanha"],
                "conjunto":  d["conjunto"],
                "thumbnail": d["thumbnail"],
                "permalink": d["permalink"],
                "gasto":     round(d["gasto"], 2),
                "leads":     int(round(d["leads"])),
                "cpl":       cpl_c,
            })

    return {
        "mes":             mes_atual,
        "mes_label":       mes_label,
        # Leads do mês
        "leads_mes": {
            "total":    total_leads_mes,
            "pago":     pago_leads_mes,
            "organico": organico_leads_mes,
            "googlecpc": google_leads_mes,
            "metaads":   meta_leads_mes,
            "direto":    direto_leads_mes,
        },
        # Custos e CPL
        "custos_mes": {
            "meta_gasto":   round(meta_gasto_mes, 2),
            "google_gasto": round(google_gasto_mes, 2),
            "total_gasto":  round(total_gasto_mes, 2),
            "cpl_meta":     cpl_meta,
            "cpl_google":   cpl_google,
            "cpl_pago":     cpl_pago,
        },
        # CRM geral
        "crm_resumo": {
            "total_deals":          total_deals,
            "total_won":            total_won,
            "total_lost":           total_lost,
            "total_active":         total_active,
            "reuniao_pdf_total":    reuniao_pdf_total,        # snapshot histórico: deals atualmente na etapa
            "reuniao_pdf_pct":      taxa_reuniao_total,        # snapshot / total_deals
            "reuniao_pdf_ativo":    reuniao_pdf_ativo,         # ativos atualmente na etapa
            "reuniao_pdf_ativo_pct": taxa_reuniao_ativo,
            "reuniao_pdf_mes":      reuniao_pdf_mes,           # deals criados NESTE mês com etapa = Reunião PDF
            "reuniao_pdf_mes_pct":  taxa_reuniao_mes,          # % do mês: reuniao_pdf_mes / total_leads_mes
            "perdas_mes":           perdas_mes,
        },
        # Top motivos de perda
        "top_motivos_perda": top_motivos,
        # Funil ativo por etapa
        "funil_ativo": funil_ativo,
        # Leads históricos por canal
        "leads_historico": {
            "googlecpc": src_map.get("googlecpc", 0),
            "metaads":   src_map.get("metaads", 0),
            "direto":    src_map.get("direto", 0),
        },
        # Perdas por canal (histórico)
        "perdas_por_canal": crm.get("losses_by_utm_source", []),
        # Comparativo mês anterior
        "comparativo": comparativo,
        # Semáforo de saúde
        "semaforo": semaforo,
        # Top 5 criativos do mês (Meta Ads)
        "top_criativos_mes": top_criativos,
    }


def main():
    print("Gerando dados do dashboard...")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    gc = get_client()

    print("  Lendo leads...")
    leads = read_sheet(gc, "leads")
    leads = dedupe_by_id(leads, "leads")

    print("  Lendo CRM deals...")
    crm_deals = read_sheet(gc, "crm_deals")
    crm_deals = dedupe_by_id(crm_deals, "crm_deals")

    print("  Lendo Meta Ads...")
    meta_ads = read_sheet(gc, "meta_ads")

    print("  Lendo Google Ads...")
    google_ads = read_sheet(gc, "google_ads")

    print("  Lendo GA4...")
    ga4 = read_sheet(gc, "ga4_sessions")

    print("  Lendo Google Ads Criativos...")
    google_ads_creatives_rows = []
    try:
        google_ads_creatives_rows = read_sheet(gc, "google_ads_creatives")
    except Exception:
        print(f"    (sem aba google_ads_creatives ainda — primeira coleta vai criar)")

    print("  Agregando dados...")

    # Faz o matching utm_content ↔ anuncio UMA VEZ para Meta e Google (com data)
    print("  Matching RD Station leads <-> criativos (Meta + Google)...")
    meta_leads_daily, google_leads_daily, _match_stats = match_rd_leads_daily(
        meta_ads, google_ads_creatives_rows, leads
    )

    meta_aggregated = aggregate_meta_ads(meta_ads, leads_daily_map=meta_leads_daily)
    # Mantém agregação total no creatives (já existente — para legacy compatibility)
    attach_rd_leads_to_creatives(meta_aggregated["creatives"], meta_ads, leads)

    crm_agg     = aggregate_crm(crm_deals, leads)
    meta_agg    = meta_aggregated
    google_agg  = aggregate_google_ads(google_ads)
    utm_agg     = aggregate_utm(leads)
    leads_agg   = aggregate_leads(leads)

    summary = {
        "last_update": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "totals": {
            "leads": len(leads),
            "crm_deals": len(crm_deals),
            "meta_rows": len(meta_ads),
            "google_rows": len(google_ads),
            "ga4_rows": len(ga4),
        },
        "leads": leads_agg,
        "crm": crm_agg,
        "meta_ads": meta_agg,
        "google_ads": google_agg,
        "ga4": aggregate_ga4(ga4),
        "utm": utm_agg,
        "google_ads_creatives": aggregate_google_ads_creatives(google_ads_creatives_rows, leads_daily_map=google_leads_daily),
        "relatorio": build_relatorio(leads, crm_agg, meta_agg, google_agg, utm_agg, meta_rows=meta_ads),
    }

    output_file = os.path.join(OUTPUT_DIR, "summary.json")

    # Grava o arquivo local (usado apenas como artefato temporário no CI;
    # o arquivo NÃO é commitado no git — os dados vão para o Supabase Storage)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(output_file) / 1024
    print(f"  Arquivo local gerado: {output_file} ({size_kb:.1f} KB)")

    # Envia para o Supabase Storage (bucket privado 'dashboard-data')
    upload_to_supabase(output_file)

    print("Dashboard data gerado com sucesso!")


if __name__ == "__main__":
    main()
