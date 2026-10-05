(() => {
  const valid = value => value === 'en' || value === 'ko';
  const key = 'telegram-bridge-docs-language';
  const picker = document.getElementById('language');
  const url = new URL(location.href);
  let saved;
  try { saved = localStorage.getItem(key); } catch {}
  const preferred = url.searchParams.get('lang');
  const initial = valid(preferred) ? preferred : valid(saved) ? saved :
    navigator.language?.startsWith('ko') ? 'ko' : 'en';

  function apply(language, remember = false) {
    if (!valid(language)) return;
    document.documentElement.lang = language;
    document.title = `${document.body.dataset.product} · ${language === 'ko' ? '사용 안내' : 'User guide'}`;
    picker.value = language;
    picker.setAttribute('aria-label', language === 'ko' ? '언어 선택' : 'Language');
    for (const element of document.querySelectorAll('[data-lang]')) {
      element.hidden = element.dataset.lang !== language;
    }
    for (const link of document.querySelectorAll('[data-section]')) {
      link.href = `#${link.dataset.section}-${language}`;
    }
    for (const link of document.querySelectorAll('[data-guide]')) {
      const target = new URL(link.href);
      target.searchParams.set('lang', language);
      link.href = target.href;
    }
    if (remember) {
      try { localStorage.setItem(key, language); } catch {}
      const current = new URL(location.href);
      current.searchParams.set('lang', language);
      current.hash = current.hash.replace(/-(en|ko)$/, `-${language}`);
      history.replaceState(null, '', current);
    }
  }
  picker.addEventListener('change', () => apply(picker.value, true));
  apply(initial);
})();
