const form = document.getElementById('costForm');
const aggregateEl = document.getElementById('aggregate');
const errorEl = document.getElementById('error');
const chartCtx = document.getElementById('costChart');

let chart;

function renderChart(series) {
  const labels = series.map((x) => x.timestamp);
  const values = series.map((x) => x.cost);

  if (chart) chart.destroy();
  chart = new Chart(chartCtx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Cost',
        data: values,
        borderColor: '#2563eb',
        backgroundColor: 'rgba(37,99,235,0.2)',
        tension: 0.25,
        fill: true,
      }],
    },
    options: {
      responsive: true,
      scales: {
        y: {
          beginAtZero: true,
        },
      },
    },
  });
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorEl.textContent = '';
  aggregateEl.textContent = 'Loading...';

  const projectId = document.getElementById('projectId').value.trim();
  const startRaw = document.getElementById('start').value;
  const endRaw = document.getElementById('end').value;
  const resolution = document.getElementById('resolution').value;

  const params = new URLSearchParams({ resolution, include_series: 'true' });
  if (startRaw) params.set('start', new Date(startRaw).toISOString());
  if (endRaw) params.set('end', new Date(endRaw).toISOString());

  try {
    const response = await fetch(`/api/projects/${projectId}/costs?${params}`);
    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }

    aggregateEl.textContent = `${data.aggregate_cost_now.toFixed(4)} ${data.currency}`;
    renderChart(data.time_series || []);
  } catch (error) {
    aggregateEl.textContent = '--';
    errorEl.textContent = error.message;
  }
});
