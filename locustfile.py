from locust import HttpUser, task, between


class URLShortenerUser(HttpUser):
    host = "http://localhost:8000"
    wait_time = between(0.1, 0.5)

    def on_start(self):
        self.created_codes = []

    @task(2)
    def create_short_url(self):
        response = self.client.post("/shorten", json={
            "original_url": "https://example.com/test"
        })
        if response.status_code == 201:
            data = response.json()
            short_code = data["short_url"].split("/")[-1]
            self.created_codes.append(short_code)

    @task(7)
    def redirect(self):
        if self.created_codes:
            import random
            code = random.choice(self.created_codes)
            self.client.get(f"/{code}", name="/[code]", allow_redirects=False)

    @task(1)
    def get_stats(self):
        if self.created_codes:
            import random
            code = random.choice(self.created_codes)
            self.client.get(f"/stats/{code}", name="/stats/{code}")
