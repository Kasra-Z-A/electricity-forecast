try {
  const theme = localStorage.getItem("theme");
  if (theme === "dark" || theme === "light")
    document.documentElement.dataset.theme = theme;
} catch {}
