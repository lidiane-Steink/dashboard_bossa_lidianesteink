import sys
# Força output sem buffer pra ver progresso em tempo real (GitHub Actions e logs)
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from collectors import rdstation, sprinthub, ads_sheets, ga4, google_ads, meta_ads
from sheets import writer
from datetime import datetime


def run():
    print("=" * 50)
    print(f"Dashboard Marketing — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 50)

    # RD Station
    print("\n[1/3] RD Station")
    try:
        leads = rdstation.get_all_leads()
        leads.sort(key=lambda x: x.get("criado_em") or "", reverse=True)
        writer.write_sheet("leads", leads)
    except Exception as e:
        print(f"  ERRO RD Station: {e}")

    # SprintHub CRM
    print("\n[2/4] SprintHub CRM")
    try:
        crm_data = sprinthub.get_all_crm_data()
        writer.write_sheet("crm_deals", crm_data["deals"])
        writer.write_sheet("crm_atividades", crm_data["atividades"])
        writer.write_sheet("crm_tarefas", crm_data["tarefas"])
    except Exception as e:
        print(f"  ERRO SprintHub CRM: {e}")

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

    # Planilha externa como fallback se APIs falharam
    if not google_api_ok or not meta_api_ok:
        print("\n[6/6] Planilhas fallback")
        if not google_api_ok:
            try:
                google_data = ads_sheets.get_google_ads_sheet_data()
                if google_data:
                    writer.write_sheet("google_ads", google_data)
                    google_api_ok = True
            except Exception as e:
                print(f"  ERRO Google Ads planilha diária: {e}")
        if not meta_api_ok:
            try:
                meta_data, _ = ads_sheets.get_ads_data()
                writer.write_sheet("meta_ads", meta_data)
            except Exception as e:
                print(f"  ERRO Meta Ads planilha: {e}")
    else:
        print("\n[6/6] APIs OK — planilhas fallback não necessárias")

    # Google Ads creatives (texto por anúncio — sem thumbnail por limitação da planilha)
    print("\n[7/7] Google Ads Criativos")
    try:
        creatives_data = ads_sheets.get_google_ads_creatives_sheet_data()
        if creatives_data:
            writer.write_sheet("google_ads_creatives", creatives_data)
    except Exception as e:
        print(f"  ERRO Google Ads Criativos: {e}")

    print("\n" + "=" * 50)
    print("Coleta finalizada com sucesso!")
    print("=" * 50)


if __name__ == "__main__":
    run()
