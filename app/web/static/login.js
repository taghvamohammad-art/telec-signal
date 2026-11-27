'use strict';
const f = document.getElementById('f'), err = document.getElementById('err');
f.addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(f);
  if (!fd.get('username') || !fd.get('password')) { err.textContent = 'نام کاربری و رمز را وارد کنید.'; return; }
  err.textContent = '';
  f.querySelector('button').disabled = true;
  try {
    const r = await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: fd.get('username'), password: fd.get('password')})});
    if (r.ok) { location.href = '/'; return; }
    err.textContent = (await r.json()).error || 'ورود انجام نشد.';
  } catch { err.textContent = 'سرور در دسترس نیست. اتصال اینترنت را بررسی کنید.'; }
  f.querySelector('button').disabled = false;
});
