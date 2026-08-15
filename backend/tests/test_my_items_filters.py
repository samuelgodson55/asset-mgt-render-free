
def test_my_items_filters_are_server_side(as_staff):
    client, headers = as_staff
    all_items = client.get("/api/users/me/items", headers=headers, params={"limit": 1, "offset": 0})
    assert all_items.status_code == 200
    assert all_items.json()["total"] >= len(all_items.json()["assigned_items"])

    overdue = client.get("/api/users/me/items", headers=headers, params={"filter": "overdue", "limit": 1, "offset": 0})
    assert overdue.status_code == 200
    assert all(item["overdue"] for item in overdue.json()["assigned_items"])

    due_soon = client.get("/api/users/me/items", headers=headers, params={"filter": "due_soon", "limit": 1, "offset": 0})
    assert due_soon.status_code == 200
    assert all(item["due_soon"] and not item["overdue"] for item in due_soon.json()["assigned_items"])
