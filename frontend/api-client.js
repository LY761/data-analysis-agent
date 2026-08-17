(function () {
  async function request(path, options = {}) {
    const response = await fetch('/api' + path, options);
    const contentType = response.headers.get('content-type') || '';
    const data = contentType.includes('application/json')
      ? await response.json().catch(() => ({}))
      : await response.text();
    if (!response.ok) {
      const detail = typeof data === 'object' ? data.detail || data : data;
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function query(path, params = {}) {
    const search = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      const values = Array.isArray(value) ? value : [value];
      values.filter(item => item !== '' && item !== null && item !== undefined)
        .forEach(item => search.append(key, item));
    });
    const suffix = search.toString();
    return request(path + (suffix ? '?' + suffix : ''));
  }

  function json(path, body, method = 'POST') {
    return request(path, {
      method,
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  }

  function upload(path, formData) {
    return request(path, {method: 'POST', body: formData});
  }

  window.WorkbenchApi = {request, query, json, upload};
})();
