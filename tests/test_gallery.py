from .conftest import client_as


def test_gallery_is_public_and_server_rendered(seeded, fixture_data):
    resp = client_as().get("/projects")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/html")
    body = resp.content.decode()
    for project in fixture_data["projects"][:3]:
        assert project["title"] in body


def test_gallery_search_by_title(seeded):
    body = client_as().get("/projects", {"q": "glass"}).content.decode()
    assert "Glass Signal" in body
    assert "Small Meadow" not in body


def test_gallery_filter_by_track(seeded, fixture_data):
    track = fixture_data["tracks"][0]
    body = client_as().get("/projects", {"track": track["id"]}).content.decode()
    in_track = {p["title"] for p in fixture_data["projects"] if p["track"] == track["id"]}
    out_of_track = {p["title"] for p in fixture_data["projects"]} - in_track
    assert all(t in body for t in in_track)
    assert not any(f">{t}<" in body for t in out_of_track)


def test_gallery_loads_no_network_assets(seeded):
    # Links out to project repos are content; anything the browser would
    # fetch on its own (scripts, stylesheets, fonts, images) must be local.
    body = client_as().get("/projects").content.decode()
    for fetched in ('src="http', 'src="//', "url(http", "url(//", "@import"):
        assert fetched not in body
    assert 'rel="stylesheet" href="http' not in body


def test_root_points_at_gallery(seeded):
    resp = client_as().get("/")
    assert resp.status_code == 302
    assert resp["Location"] == "/projects"
