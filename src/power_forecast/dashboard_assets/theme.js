// The theme choice is a cookie on kasra.page, shared with the website and the
// other demos, so following a link keeps the theme; localStorage is the
// fallback where that cookie can't be set (localhost). Same logic as the
// website's Base.astro; keep the two in step.
window.readTheme = () => {
  const match = document.cookie.match(/(?:^|;\s*)theme=(light|dark)(?:;|$)/);
  if (match) return match[1];
  try {
    const stored = localStorage.getItem("theme");
    if (stored === "dark" || stored === "light") return stored;
  } catch {}
  return null;
};
window.storeTheme = (theme) => {
  try {
    localStorage.setItem("theme", theme);
  } catch {}
  if (/(^|\.)kasra\.page$/.test(location.hostname)) {
    document.cookie = `theme=${theme}; Domain=kasra.page; Path=/; Max-Age=31536000; SameSite=Lax; Secure`;
  }
};
{
  const theme = window.readTheme();
  if (theme) {
    window.storeTheme(theme);
    document.documentElement.dataset.theme = theme;
  }
}
