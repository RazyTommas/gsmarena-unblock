/** The only frontend/backend boundary. UI code never accesses storage directly. */
const BASE = (globalThis.OBSERVATORY_API_BASE || '/api/v1').replace(/\/$/, '');

export class ApiError extends Error {
  constructor(message, status) { super(message); this.name = 'ApiError'; this.status = status; }
}

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { Accept: 'application/json', 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!response.ok) throw new ApiError(`Request failed (${response.status})`, response.status);
  return response.status === 204 ? null : response.json();
}

const params = input => {
  const query = new URLSearchParams(Object.entries(input || {}).filter(([, value]) => value !== '' && value != null));
  return query.size ? `?${query}` : '';
};

export const api = {
  overview: () => request('/radar/overview'),
  updates: filters => request(`/updates${params(filters)}`),
  acknowledge: id => request(`/updates/${encodeURIComponent(id)}/acknowledge`, { method: 'POST' }),
  acknowledgeMany: ids => request('/updates/acknowledge-bulk', { method: 'POST', body: JSON.stringify({ ids }) }),
  acknowledgements: () => request('/updates/acknowledgements'),
  deviceDetail: model => request(`/devices/${encodeURIComponent(model)}`),
  devices: filters => request(`/devices${params(filters)}`),
  productDetail: id => request(`/products/${encodeURIComponent(id)}`),
  chipProducts: filters => request(`/chips/products${params(filters)}`),
  chips: filters => request(`/chips${params(filters)}`),
  releases: filters => request(`/releases${params(filters)}`),
  productReleases: filters => request(`/product-releases${params(filters)}`),
  productSecurity: filters => request(`/product-security${params(filters)}`),
  sourceRecords: filters => request(`/source-records${params(filters)}`),
  sourceProducts: filters => request(`/identity/products${params(filters)}`),
  reviewSourceProduct: (id, decision) => request(`/identity/products/${encodeURIComponent(id)}/review`, {method:'POST',body:JSON.stringify({decision})}),
  agentBundle: () => request('/identity/agent-bundle'),
  securityDetail: cve => request(`/security/cves/${encodeURIComponent(cve)}`),
  security: filters => request(`/security/findings${params(filters)}`),
  securityCoverage: () => request('/security/coverage'),
  health: () => request('/admin/health'),
  search: query => request(`/search${params({ q: query })}`),
  config: () => request('/admin/config'),
  configOptions: () => request('/admin/options'),
  realSample: () => request('/admin/real-sample'),
  reviewProfiles: () => request('/admin/review-profiles'),
  saveConfig: config => request('/admin/config', { method: 'POST', body: JSON.stringify(config) }),
  identityDecisions: () => request('/identity/decisions'),
  saveIdentityDecision: value => request('/identity/decisions', { method: 'POST', body: JSON.stringify(value) }),
  collectionRequests: () => request('/admin/collection-requests'),
  requestCollection: value => request('/admin/collection-requests', { method: 'POST', body: JSON.stringify(value) }),
  processNextCollection: () => request('/admin/collection-requests/process-next', { method: 'POST', body: '{}' }),
};

export { BASE as API_BASE };
