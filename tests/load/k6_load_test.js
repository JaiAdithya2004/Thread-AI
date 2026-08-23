import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const errorRate = new Rate('errors');
const searchLatency = new Trend('search_latency', true);

export const options = {
  stages: [
    { duration: '30s', target: 10 },   // ramp up to 10 users
    { duration: '60s', target: 20 },   // sustain 20 users
    { duration: '30s', target: 0 },    // ramp down
  ],
  thresholds: {
    'search_latency': ['p(95)<300'],   // p95 < 300ms requirement
    'errors': ['rate<0.01'],           // error rate < 1%
    'http_req_duration': ['p(95)<500'],
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:7070';
const AUTH = { Authorization: 'Bearer test-token-dev' };

export default function () {
  // Search query
  const searchRes = http.post(`${BASE_URL}/api/search`,
    JSON.stringify({ query: 'company decision', cross_team: false }),
    { headers: { 'Content-Type': 'application/json', ...AUTH } }
  );

  const ok = check(searchRes, {
    'search status 200': (r) => r.status === 200,
    'search has answer': (r) => {
      try { return JSON.parse(r.body).answer !== undefined; }
      catch { return false; }
    },
  });

  errorRate.add(!ok);
  searchLatency.add(searchRes.timings.duration);

  sleep(0.5);

  // Health check
  const healthRes = http.get(`${BASE_URL}/healthz`);
  check(healthRes, { 'health 200': (r) => r.status === 200 });
}
