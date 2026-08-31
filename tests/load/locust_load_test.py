"""
locust_load_test.py — Python load test for Thread API.

Usage:
    pip install locust
    locust -f tests/load/locust_load_test.py --headless -u 20 -r 5 --run-time 2m \
        --host http://localhost:7070

    # Or with the web UI:
    locust -f tests/load/locust_load_test.py --host http://localhost:7070
    # then open http://localhost:8089
"""
from locust import HttpUser, task, between


class BrainUser(HttpUser):
    wait_time = between(0.5, 2)
    headers = {
        "Authorization": "Bearer test-token-dev",
        "Content-Type": "application/json",
    }

    @task(3)
    def search(self):
        self.client.post(
            "/api/search",
            json={"query": "company policy decision", "cross_team": False},
            headers=self.headers,
        )

    @task(1)
    def health(self):
        self.client.get("/healthz")

    @task(1)
    def metrics(self):
        self.client.get("/metrics")

    @task(1)
    def facts(self):
        self.client.get(
            "/api/facts?limit=20&offset=0",
            headers=self.headers,
        )

    @task(1)
    def ledger(self):
        self.client.get(
            "/api/ledger?limit=20&offset=0",
            headers=self.headers,
        )
