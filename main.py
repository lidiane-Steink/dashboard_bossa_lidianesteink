import sys
# Força output sem buffer pra ver progresso em tempo real (GitHub Actions e logs)
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from collectors import sprinthub, ads_sheets, ga4, google_ads, meta_ads
from sheets import writer
from datetime import datetime


def run():
    print("=" * 50)
    print(f"Dashboard Marketing — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 50)

    # SprintHub — leads.
    # IMPORTANTE: /leads retorna TODOS os contatos (WhatsApp, Instagram, forms...),
    # não só leads de marketing. Por decisão do cliente, o dashboard conta apenas
    # LEADS DE CAMPANHA = contatos com UTM (vieram de anúncio Meta/Google).
    # Os contatos orgânicos (sem UTM) são descartados aqui.
    print("\n[1/6] SprintHub — leads")
    leads_raw = []
    try:
        leads_raw = sprinthub.get_leads_raw()
    except Exception as e:
        print(f"  ERRO coletando leads (parcial pode ter sido obtida): {e}")

    if leads_raw:
        try:
            todos = sprinthub.normalize_leads(leads_raw)
            # Mantém só leads com UTM source (campanha). Sem UTM = orgânico/WhatsApp.
            leads = [l for l in todos if (l.get("utm_source") or "").strip()]
            print(f"  Leads de campanha (com UTM): {len(leads)} de {len(todos)} contatos totais")
            leads.sort(key=lambda x: x.get("criado_em") or "", reverse=True)
            writer.write_sheet("leads", leads)
        except Exception as e:
            print(f"  ERRO ao salvar leads: {e}")

    # CRM / Oportunidades — DESABILITADO.
    # O SprintHub não tem endpoint bulk de oportunidades; buscar contato a contato
    # nos 14k+ contatos é inviável (lento + rate limit). O funil do CRM fica de fora
    # por ora (decisão do cliente: focar em leads de campanha).
    print("\n[2/6] SprintHub — CRM (deals): desabilitado (sem endpoint bulk)")

    # Google Analytics 4
    print("\n[3/6] Google Analytics 4")
    try:
        ga4_data = ga4.get_ga4_data()
        writer.write_sheet("ga4_sessions", ga4_data)
    except Exception as e:
        print(f"  ERRO GA4: {e}")

    # Google Ads API (inclui Performance Max via FROM campaign)
    print("\n[4/6] Google Ads API")
    google_api_ok = False
    try:
        google_data = google_ads.get_campaign_data(days_back=90)
        if google_data:
            writer.write_sheet("google_ads", google_data)
            google_api_ok = True
    except Exception as e:
        print(f"  ERRO Google Ads API: {e}")

    # Meta Ads API
    print("\n[5/6] Meta Ads API")
    meta_api_ok = False
    try:
        meta_data = meta_ads.get_campaign_data(days_back=90)
        if meta_data:
            writer.write_sheet("meta_ads", meta_data)
            meta_api_ok = True
    except Exception as e:
        print(f"  ERRO Meta Ads API: {e}")

    # Planilhas de Ads do Ouro do Gege — abas 'Meta' e 'Google' na planilha principal
    print("\n[6/6] Planilhas Meta + Google Ads")
    try:
        google_data = ads_sheets.get_google_ads_sheet_data()
        if google_data:
            writer.write_sheet("google_ads", google_data)
    except Exception as e:
        print(f"  ERRO Google Ads (planilha): {e}")

    try:
        meta_data = ads_sheets.get_meta_ads_sheet_data()
        if meta_data:
            writer.write_sheet("meta_ads", meta_data)
    except Exception as e:
        print(f"  ERRO Meta Ads (planilha): {e}")

    print("\n" + "=" * 50)
    print("Coleta finalizada com sucesso!")
    print("=" * 50)


if __name__ == "__main__":
    run()
