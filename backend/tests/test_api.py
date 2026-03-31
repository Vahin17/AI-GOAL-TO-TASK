from fastapi.testclient import TestClient
from backend.main import app

client = TestClient(app)

def test_health_check_endpoint():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "planning-engine"}

def test_plan_generation_fallback_route():
    # Sending a test payload
    response = client.post(
        "/api/v1/plan",
        json={"goal": "Make a to-do list application"}
    )
    
    # Ensuring route connects and API doesn't crash
    assert response.status_code == 200
    
    # Asserting JSON response accurately maps to the strict schema returned by fallback
    json_data = response.json()
    assert "nodes" in json_data
    assert "edges" in json_data
    assert "metadata" in json_data
    assert len(json_data["nodes"]) == 3
