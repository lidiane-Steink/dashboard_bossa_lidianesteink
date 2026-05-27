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

    # SprintHub — leads + CRM (única fonte para o Ouro do Gege)
    # Coletamos os leads UMA VEZ e reusamos a lista para deals (evita duplicar chamada).
    print("\n[1/6] SprintHub — leads")
    leads_raw = []
    try:
        leads_raw = sprinthub.get_leads_raw()
    except Exception as e:
        print(f"  ERRO coletando leads (parcial pode ter sido obtida): {e}")

    # Mesmo que tenha parado no meio, salva o que conseguiu
    if leads_raw:
        try:
            leads = sprinthub.normalize_leads(leads_raw)
            leads.sort(key=lambda x: x.get("criado_em") or "", reverse=True)
            writer.write_sheet("leads", leads)
        except Exception as e:
            print(f"  ERRO ao salvar leads: {e}")

    print("\n[2/6] SprintHub — CRM (deals)")
    if leads_raw:
        try:
            deals = sprinthub.get_deals_from_leads(leads_raw)
            if deals:
                writer.write_sheet("crm_deals", deals)
        except Exception as e:
            print(f"  ERRO SprintHub deals: {e}")
    else:
        print("  Pulado — sem leads coletados.")

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
