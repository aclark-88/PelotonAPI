"""One-time HeyReach infrastructure setup — the canonical campaign.

Creates (idempotently, by name lookup):
  - lead list  "Clarion GTM Launch Leads"
  - campaign   "Clarion GTM - Launch Outreach"  with the sequence
        CONNECTION_REQUEST  message: {{cr_note}}        (per-lead custom field)
          on accept -> MESSAGE: {{followup}}  (3 days)  (per-lead custom field)
          timeout   -> END (withdraw after 30 days)

While the campaign is still DRAFT (nothing can send), it verifies that
AddLeadsToCampaignV2 accepts customUserFields by adding and immediately
removing a sentinel lead. Then starts the campaign (empty = inert) and prints
the id to pin in configs/heyreach_dispatcher.yaml.

    py -m gtm.cli.setup_heyreach
"""

from __future__ import annotations

import sys

from gtm.skills._shared.heyreach import HeyReachClient

LIST_NAME = "Clarion GTM Launch Leads"
CAMPAIGN_NAME = "Clarion GTM - Launch Outreach"
SENTINEL = "https://www.linkedin.com/in/heyreach-setup-sentinel-do-not-send"


def main() -> int:
    client = HeyReachClient()

    sender = client.find_sender("alex")
    if sender is None:
        print("FATAL: no LinkedIn sender account matching 'alex' in the workspace")
        return 1
    print(f"sender: {sender['firstName']} {sender.get('lastName', '')} (id {sender['id']})")

    existing = client.find_campaign(CAMPAIGN_NAME)
    if existing:
        print(f"canonical campaign already exists: id={existing['id']} "
              f"status={existing.get('status')} — nothing to do")
        print(f"\npin in configs/heyreach_dispatcher.yaml -> canonical_campaign_id: {existing['id']}")
        return 0

    created_list = client.create_empty_list(LIST_NAME)
    list_id = int(created_list["id"])
    print(f"list created: id={list_id}")

    campaign = client.create_cr_campaign(
        name=CAMPAIGN_NAME,
        linkedin_account_ids=[int(sender["id"])],
        list_id=list_id,
        cr_note="{{cr_note}}",
        followup_message="{{followup}}",
        followup_delay_days=3,
        withdraw_after_days=30,
        exclude_contacted=True,
    )
    campaign_id = int(campaign["campaignId"])  # NB: key is campaignId, not id
    print(f"campaign created (DRAFT): id={campaign_id}")

    # Leads can only append to a STARTED campaign ('You cannot add new leads
    # to a draft campaign', verified 2026-06-11). Start empty (inert), then
    # verify customUserFields with an invalid-URL sentinel (silently skipped
    # by HeyReach senders, so nothing can actually go out).
    client.start_campaign(campaign_id)
    print("campaign started (empty)")
    try:
        client.add_leads_to_campaign(
            campaign_id, int(sender["id"]),
            lead={"profileUrl": SENTINEL, "firstName": "Sentinel", "lastName": "Test"},
            custom_fields={"cr_note": "sentinel cr", "followup": "sentinel followup"},
        )
        print("customUserFields accepted by AddLeadsToCampaignV2 ✓")
    except Exception as exc:
        print(f"WARNING: custom-fields verification failed: {exc}")
        return 1
    finally:
        try:
            client.stop_lead_in_campaign(campaign_id, SENTINEL)
            print("sentinel lead stopped ✓")
        except Exception as exc:
            print(f"NOTE: sentinel stop returned: {exc}")

    print(f"\npin in configs/heyreach_dispatcher.yaml -> canonical_campaign_id: {campaign_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
