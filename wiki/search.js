(() => {
  const input = document.getElementById('wiki-search');
  const groups = document.querySelectorAll('.wiki-group');
  const noResults = document.getElementById('no-results');
  if (!input) return;

  input.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    let anyMatch = false;

    groups.forEach(group => {
      let groupMatch = false;
      group.querySelectorAll('.wiki-card').forEach(card => {
        const title = card.querySelector('.card-title').textContent;
        const desc = card.querySelector('.card-desc').textContent;
        const match = !q || (title + ' ' + desc).toLowerCase().includes(q);
        card.style.display = match ? '' : 'none';
        if (match) groupMatch = true;
      });
      group.style.display = groupMatch ? '' : 'none';
      if (groupMatch) anyMatch = true;
    });

    if (q && !anyMatch) {
      noResults.textContent = `No pages match "${input.value.trim()}".`;
      noResults.style.display = '';
    } else {
      noResults.style.display = 'none';
    }
  });
})();
