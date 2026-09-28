// Queue page: whole rows are clickable, and the page refreshes when tickets change.
(function () {
  document.querySelectorAll("tr.row-link").forEach(function (row) {
    row.addEventListener("click", function (e) {
      if (e.target.closest("a, button, form")) return;
      window.location = row.dataset.href;
    });
  });

  if (document.body.dataset.page !== "queue") return;
  var kpis = document.getElementById("kpis");
  if (!kpis) return;
  var seen = kpis.dataset.active + "/" + kpis.dataset.emergency + "/" + kpis.dataset.unclaimed;
  setInterval(function () {
    fetch("/desk/api/summary", { credentials: "same-origin" })
      .then(function (r) { if (r.status === 401) { window.location = "/desk/login"; } return r.json(); })
      .then(function (s) {
        if (!s) return;
        var now = s.active + "/" + s.emergency + "/" + s.unclaimed;
        if (now !== seen) window.location.reload();
      })
      .catch(function () {});
  }, 20000);
})();
