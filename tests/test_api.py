NEW = {
    "carrier_scac": "NRFX", "tracking_number": "API-TEST-0001", "supplier_code": "SUP-00001",
    "warehouse_code": "DC-ATL", "ship_date": "2026-11-01", "promised_date": "2026-11-05",
    "weight_kg": 120.5, "freight_cost_cents": 9900,
}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["shipments"] == 8000


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Shipment Analytics" in r.text


def test_create_and_lookup(client):
    r = client.post("/shipments", json=NEW)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["carrier_scac"] == "NRFX" and body["status"] == "in_transit" and body["delay_days"] is None
    r = client.get("/shipments/lookup", params={"carrier_scac": "nrfx", "tracking_number": "API-TEST-0001"})
    assert r.status_code == 200 and r.json()["shipment_id"] == body["shipment_id"]


def test_duplicate_same_carrier_is_409(client):
    assert client.post("/shipments", json=NEW).status_code == 201
    r = client.post("/shipments", json=NEW)
    assert r.status_code == 409


def test_same_tracking_other_carrier_is_ok(client):
    assert client.post("/shipments", json=NEW).status_code == 201
    assert client.post("/shipments", json={**NEW, "carrier_scac": "KPAR"}).status_code == 201


def test_validation_errors(client):
    assert client.post("/shipments", json={**NEW, "promised_date": "2026-10-01"}).status_code == 422
    assert client.post("/shipments", json={**NEW, "status": "delivered"}).status_code == 422
    assert client.post("/shipments", json={**NEW, "carrier_scac": "ZZZZ"}).status_code == 422
    assert client.post("/shipments", json={**NEW, "weight_kg": 0}).status_code == 422


def test_delivered_insert_updates_rollups(client):
    params = {"date_from": "2026-11-10", "date_to": "2026-11-10"}
    before = client.get("/analytics/summary", params=params).json()
    late = {**NEW, "status": "delivered", "delivered_date": "2026-11-10"}  # 5 days late
    assert client.post("/shipments", json=late).status_code == 201
    after = client.get("/analytics/summary", params=params).json()
    live = client.get("/analytics/summary", params={**params, "source": "live"}).json()
    assert after["delivered"] == before["delivered"] + 1
    assert after["late"] == before["late"] + 1
    assert after["total_delay_days"] == before["total_delay_days"] + 5
    assert {k: after[k] for k in ("delivered", "late", "total_delay_days")} == \
           {k: live[k] for k in ("delivered", "late", "total_delay_days")}


def test_patch_to_delivered_updates_rollups(client):
    sid = client.post("/shipments", json=NEW).json()["shipment_id"]
    r = client.patch(f"/shipments/{sid}", json={"status": "delivered", "delivered_date": "2026-11-04"})
    assert r.status_code == 200 and r.json()["delay_days"] == -1
    p = {"date_from": "2026-11-04", "date_to": "2026-11-04"}
    assert client.get("/analytics/summary", params=p).json()["delivered"] == \
           client.get("/analytics/summary", params={**p, "source": "live"}).json()["delivered"]
    # inconsistent update is rejected by the CHECK constraint
    assert client.patch(f"/shipments/{sid}", json={"status": "in_transit", "delivered_date": "2026-11-04"}).status_code == 422


def test_keyset_pagination(client):
    seen, after = [], None
    for _ in range(3):
        params = {"limit": 100, "status": "delivered"} | ({"after_id": after} if after else {})
        page = client.get("/shipments", params=params).json()
        seen += [s["shipment_id"] for s in page["items"]]
        after = page["next_after_id"]
    assert len(seen) == 300 and seen == sorted(set(seen))


def test_late_only_filter(client):
    items = client.get("/shipments", params={"late_only": True, "limit": 200}).json()["items"]
    assert items and all(s["delay_days"] > 0 for s in items)


def test_analytics_endpoints(client):
    s = client.get("/analytics/summary").json()
    assert 0 < s["on_time_rate"] < 1
    carriers = client.get("/analytics/delays/by-carrier").json()
    assert len(carriers) == 18
    assert [c["on_time_rate"] for c in carriers] == sorted(c["on_time_rate"] for c in carriers)
    trend = client.get("/analytics/delays/trend", params={"date_from": "2025-01-01", "date_to": "2025-12-31"}).json()
    assert [t["month"] for t in trend] == [f"2025-{m:02d}" for m in range(1, 13)]
    worst = client.get("/analytics/delays/by-supplier", params={"limit": 5}).json()
    best = client.get("/analytics/delays/by-supplier", params={"limit": 5, "order": "best"}).json()
    assert worst[0]["on_time_rate"] <= best[0]["on_time_rate"]
    assert client.get("/analytics/delays/by-supplier",
                      params={"date_from": "2025-01-15"}).status_code == 422          # rollup needs whole months
    assert client.get("/analytics/delays/by-supplier",
                      params={"date_from": "2025-01-15", "source": "live"}).status_code == 200
    assert client.get("/analytics/summary",
                      params={"date_from": "2025-02-01", "date_to": "2025-01-01"}).status_code == 422


def test_supplier_scorecard(client):
    card = client.get("/suppliers/1").json()
    assert card["supplier_code"] == "SUP-00001"
    assert card["totals"]["delivered"] == card["shipments_by_status"].get("delivered", 0)
    assert client.get("/suppliers/999999").status_code == 404


def test_read_only_mode_blocks_writes(client, monkeypatch):
    monkeypatch.setenv("DEMO_READ_ONLY", "1")
    assert client.post("/shipments", json=NEW).status_code == 403
    assert client.patch("/shipments/1", json={"status": "cancelled"}).status_code == 403
    assert client.post("/admin/refresh-metrics").status_code == 403
    # reads still work
    h = client.get("/health").json()
    assert h["read_only"] is True and h["shipments"] == 8000
    assert client.get("/analytics/summary").status_code == 200


def test_health_reports_latest_delivered_date(client):
    latest = client.get("/health").json()["latest_delivered_date"]
    newest = client.get("/analytics/delays/trend", params={"source": "live"}).json()[-1]["month"]
    assert latest[:7] == newest
