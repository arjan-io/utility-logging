/* Small self-contained SVG charts: columns for totals, a spline for five-year averages. */
const COLORS = {
  teal: '#006c73', blue: '#2854bd', gold: '#c77a00',
  red: '#ba3842', green: '#187a40',
};
const YEAR_COLORS = ['#315d72', '#7853a6', '#007e79', '#c77a00', '#2854bd'];
const SVG_NS = 'http://www.w3.org/2000/svg';
const svgNode = (tag, attrs = {}) => {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
};
const euro = value => value == null ? '—' :
  new Intl.NumberFormat('nl-NL', {style: 'currency', currency: 'EUR'}).format(value);
const valueText = (value, digits = 1) => value == null ? '—' :
  new Intl.NumberFormat('nl-NL', {maximumFractionDigits: digits}).format(value);
const barValue = value => new Intl.NumberFormat('nl-NL', {
  minimumFractionDigits: 1, maximumFractionDigits: 1,
}).format(value);
const localStamp = value => value ?
  new Date(value).toLocaleString('nl-NL', {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
    timeZone: 'Europe/Amsterdam',
  }) : 'geen meting';
const localTime = value => new Date(value).toLocaleTimeString('nl-NL', {
  hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Amsterdam',
});
const dateLabel = value => new Date(value + 'T12:00:00').toLocaleDateString('nl-NL', {
  day: 'numeric', month: 'short',
});
const monthLabel = value => new Date(value + 'T12:00:00').toLocaleDateString('nl-NL', {
  month: 'short',
});

function linePath(points, smooth) {
  if (!points.length) return '';
  let path = `M ${points[0][0]} ${points[0][1]}`;
  for (let i = 1; i < points.length; i++) {
    const [x, y] = points[i];
    if (!smooth) {
      path += ` L ${x} ${y}`;
      continue;
    }
    const previous = points[i - 1];
    const before = points[i - 2] || previous;
    const after = points[i + 1] || points[i];
    const c1x = previous[0] + (x - before[0]) / 6;
    const c1y = previous[1] + (y - before[1]) / 6;
    const c2x = x - (after[0] - previous[0]) / 6;
    const c2y = y - (after[1] - previous[1]) / 6;
    path += ` C ${c1x} ${c1y}, ${c2x} ${c2y}, ${x} ${y}`;
  }
  return path;
}

function chart(target, title, rows, series, options = {}) {
  const host = document.getElementById(target);
  host.replaceChildren();
  const width = 760, height = 230, left = 48, right = 17, top = 14, bottom = 35;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const barSeries = series.filter(item => item.type !== 'spline' && options.mode !== 'line');
  const lineSeries = options.mode === 'line' ? series : series.filter(item => item.type === 'spline');
  const visible = (row, item) => row[item.key] != null && Number.isFinite(row[item.key]) &&
    (!item.quality || !['unknown', 'not_collected'].includes(row[item.quality]));
  const values = rows.map(row => options.mode === 'stacked'
    ? barSeries.every(item => visible(row, item))
      ? barSeries.reduce((sum, item) => sum + row[item.key], 0) : 0
    : Math.max(0, ...series.map(item => visible(row, item) ? row[item.key] : 0)));
  const hasValue = rows.some(row => options.mode === 'stacked'
    ? barSeries.every(item => visible(row, item))
    : series.some(item => visible(row, item)));
  if (!hasValue) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = 'Voor deze periode zijn geen metingen beschikbaar.';
    host.append(empty);
    return;
  }
  const maximum = Math.max(0.01, ...values) * 1.12;
  const y = value => top + plotHeight - value / maximum * plotHeight;
  const x = index => left + (index + 0.5) * plotWidth / rows.length;
  const labelAt = index => options.time ? localTime(rows[index].time_utc)
    : options.year ? rows[index].date.slice(0, 4)
    : options.month ? rows[index].date.slice(0, 7)
    : options.compare ? monthLabel(rows[index].date)
    : dateLabel(rows[index].date);
  const svg = svgNode('svg', {
    viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': title,
  });
  host.append(svg);
  for (let index = 0; index <= 4; index++) {
    const tickY = top + plotHeight * index / 4;
    svg.append(svgNode('line', {
      x1: left, x2: width - right, y1: tickY, y2: tickY, class: 'gridline',
    }));
    const label = svgNode('text', {
      x: left - 7, y: tickY + 4, 'text-anchor': 'end', class: 'axis',
    });
    label.textContent = valueText(maximum * (4 - index) / 4, options.digits ?? 1);
    svg.append(label);
  }
  const every = Math.max(1, Math.ceil(rows.length / (options.compare ? 12 : 7)));
  rows.forEach((row, index) => {
    if (index % every !== 0 && index !== rows.length - 1) return;
    const label = svgNode('text', {
      x: x(index), y: height - 9, 'text-anchor': 'middle', class: 'axis',
    });
    label.textContent = labelAt(index);
    svg.append(label);
  });

  const slotWidth = plotWidth / rows.length;
  const groupedWidth = Math.min(slotWidth * 0.8, 52);
  rows.forEach((row, index) => {
    if (options.mode === 'stacked' && !barSeries.every(item => visible(row, item))) return;
    let stack = 0;
    barSeries.forEach((item, seriesIndex) => {
      const value = row[item.key];
      if (!visible(row, item)) return;
      const quality = item.quality ? row[item.quality] : null;
      const barWidth = options.mode === 'stacked'
        ? groupedWidth
        : groupedWidth / Math.max(barSeries.length, 1);
      const barX = options.mode === 'stacked'
        ? x(index) - groupedWidth / 2
        : x(index) - groupedWidth / 2 + seriesIndex * barWidth;
      const barTop = y(options.mode === 'stacked' ? stack + value : value);
      const barHeight = Math.max(0, y(options.mode === 'stacked' ? stack : 0) - barTop);
      const rect = svgNode('rect', {
        x: barX, y: barTop, width: Math.max(1, barWidth - 1),
        height: barHeight, fill: item.color,
        class: `chart-bar ${quality === 'estimated' ? 'estimated' : ''}`,
      });
      const tooltip = svgNode('title');
      tooltip.textContent = `${labelAt(index)} · ${item.label}: ${valueText(value, 3)} ${options.unit || ''}${quality === 'estimated' ? ' · schatting' : ''}`;
      rect.append(tooltip);
      svg.append(rect);
      if (value > 0 && barHeight >= (barWidth < 38 ? 34 : 18)) {
        const labelX = barX + barWidth / 2;
        const labelY = barTop + barHeight / 2;
        const label = svgNode('text', {
          x: labelX, y: labelY,
          'text-anchor': 'middle', 'dominant-baseline': 'middle',
          class: 'bar-value',
        });
        label.textContent = value < 0.05 ? '<0,1' : barValue(value);
        if (barWidth < 38) label.setAttribute('transform',
          `rotate(-90 ${labelX} ${labelY})`);
        svg.append(label);
      }
      if (options.mode === 'stacked') stack += value;
    });
  });

  lineSeries.forEach(item => {
    let segment = [];
    const finish = () => {
      if (!segment.length) return;
      svg.append(svgNode('path', {
        d: linePath(segment, item.type === 'spline'),
        stroke: item.color, class: 'chart-line',
      }));
      segment = [];
    };
    rows.forEach((row, index) => {
      const value = row[item.key];
      if (!visible(row, item)) {
        finish();
        return;
      }
      const cx = x(index), cy = y(value);
      segment.push([cx, cy]);
      const circle = svgNode('circle', {
        cx, cy, r: item.type === 'spline' ? 4 : rows.length > 40 ? 2.3 : 3.2,
        fill: item.type === 'spline' ? '#fff' : item.color,
        stroke: item.color,
        class: `chart-dot ${row[item.quality] === 'estimated' ? 'estimated' : ''}`,
      });
      const tooltip = svgNode('title');
      tooltip.textContent = `${labelAt(index)} · ${item.label}: ${valueText(value, 3)} ${options.unit || ''}`;
      circle.append(tooltip);
      svg.append(circle);
    });
    finish();
  });
  const legend = document.createElement('div');
  legend.className = 'legend';
  series.forEach(item => {
    const entry = document.createElement('span');
    entry.style.setProperty('--color', item.color);
    entry.textContent = item.label;
    legend.append(entry);
  });
  host.append(legend);
}

function panel(parent, title, eyebrowText, id, wide = false) {
  const article = document.createElement('article');
  article.className = 'panel' + (wide ? ' wide' : '');
  const heading = document.createElement('div');
  heading.className = 'panel-heading';
  const intro = document.createElement('div');
  const eyebrow = document.createElement('p');
  eyebrow.className = 'eyebrow';
  eyebrow.textContent = eyebrowText;
  const titleElement = document.createElement('h2');
  titleElement.textContent = title;
  intro.append(eyebrow, titleElement);
  heading.append(intro);
  const plot = document.createElement('div');
  plot.id = id;
  plot.className = 'chart';
  article.append(heading, plot);
  document.getElementById(parent).append(article);
}

function monthlyComparison(years, key, quality) {
  const rows = Array.from({length: 12}, (_, index) => ({
    date: `2026-${String(index + 1).padStart(2, '0')}-01`,
  }));
  const series = [];
  years.forEach((year, index) => {
    const field = 'year_' + year.year;
    series.push({key: field, quality: field + '_quality',
      label: String(year.year), color: YEAR_COLORS[index]});
    year.months.forEach((month, monthIndex) => {
      rows[monthIndex][field] = ['unknown', 'not_collected'].includes(month[quality])
        ? null : month[key];
      rows[monthIndex][field + '_quality'] = month[quality];
    });
  });
  rows.forEach(row => {
    const observed = series.map(item => row[item.key]).filter(value => value != null);
    row.average = observed.length ?
      observed.reduce((sum, value) => sum + value, 0) / observed.length : null;
  });
  series.push({key: 'average', label: 'Gemiddelde', color: COLORS.red, type: 'spline'});
  return {rows, series};
}

function yearlyRows(years, keys, quality, currentMonth) {
  return years.map(year => {
    const row = {date: year.year + '-01-01'};
    for (const key of keys) {
      const required = year.year === Number(currentMonth.slice(0, 4))
        ? year.months.slice(0, Number(currentMonth.slice(5, 7)) - 1) : year.months;
      row[key] = required.length && required.every(month => month[key] != null &&
        !['unknown', 'not_collected'].includes(month[quality]))
        ? required.reduce((sum, month) => sum + month[key], 0) : null;
    }
    row[quality] = year.year !== Number(currentMonth.slice(0, 4)) &&
      year.months.every(month => month[quality] === 'complete')
      ? 'complete' : 'estimated';
    return row;
  });
}

function makeCharts(data) {
  const days = data.days, months = data.months.slice(-13), years = data.years;
  const lowHigh = [
    {key: 'import_low_kwh', quality: 'electricity_quality', label: 'Dal', color: COLORS.teal},
    {key: 'import_high_kwh', quality: 'electricity_quality', label: 'Normaal', color: COLORS.blue},
  ];
  const exportLowHigh = [
    {key: 'export_low_kwh', quality: 'electricity_quality', label: 'Dal', color: COLORS.gold},
    {key: 'export_high_kwh', quality: 'electricity_quality', label: 'Normaal', color: COLORS.red},
  ];
  chart('overview-power', 'Netafname laatste 24 uur', data.recent.import_w,
    [{key: 'value', label: 'Netafname', color: COLORS.teal}], {mode: 'line', time: true, unit: 'W', digits: 0});
  chart('overview-days', 'Stroomafname per dag', days, lowHigh,
    {mode: 'stacked', unit: 'kWh'});
  const definitions = [
    ['stroom-charts', 'Netafname', 'LAATSTE 24 UUR', 'power-import', data.recent.import_w,
      [{key: 'value', label: 'Afname', color: COLORS.teal}], {mode: 'line', time: true, unit: 'W', digits: 0}],
    ['stroom-charts', 'Teruglevering', 'LAATSTE 24 UUR', 'power-export', data.recent.export_w,
      [{key: 'value', label: 'Teruglevering', color: COLORS.gold}], {mode: 'line', time: true, unit: 'W', digits: 0}],
    ['stroom-charts', 'Stroomafname', 'LAATSTE 31 DAGEN', 'elec-days', days, lowHigh,
      {mode: 'stacked', unit: 'kWh'}],
    ['stroom-charts', 'Teruglevering', 'LAATSTE 31 DAGEN', 'export-days', days, exportLowHigh,
      {mode: 'stacked', unit: 'kWh'}],
    ['stroom-charts', 'Stroomafname', 'LAATSTE 13 VOLTOOIDE MAANDEN', 'elec-months', months, lowHigh,
      {mode: 'stacked', month: true, unit: 'kWh'}],
    ['stroom-charts', 'Teruglevering', 'LAATSTE 13 VOLTOOIDE MAANDEN', 'export-months', months, exportLowHigh,
      {mode: 'stacked', month: true, unit: 'kWh'}],
    ['gas-charts', 'Gasverbruik', 'LAATSTE 31 DAGEN', 'gas-days', days,
      [{key: 'gas_m3', quality: 'gas_quality', label: 'Gas', color: COLORS.red}], {mode: 'column', unit: 'm³'}],
    ['gas-charts', 'Gasverbruik', 'LAATSTE 13 VOLTOOIDE MAANDEN', 'gas-months', months,
      [{key: 'gas_m3', quality: 'gas_quality', label: 'Gas', color: COLORS.red}], {mode: 'column', month: true, unit: 'm³'}],
    ['water-charts', 'Water per uur', 'LAATSTE 24 UUR', 'water-hours', data.recent.water_liters_per_hour,
      [{key: 'value', label: 'Water', color: COLORS.green}], {mode: 'column', time: true, unit: 'liter', digits: 0}],
    ['water-charts', 'Waterverbruik', 'LAATSTE 31 DAGEN', 'water-days', days,
      [{key: 'water_m3', quality: 'water_quality', label: 'Water', color: COLORS.green}], {mode: 'column', unit: 'm³'}],
    ['water-charts', 'Waterverbruik', 'LAATSTE 13 VOLTOOIDE MAANDEN', 'water-months', months,
      [{key: 'water_m3', quality: 'water_quality', label: 'Water', color: COLORS.green}], {mode: 'column', month: true, unit: 'm³'}],
    ['zon-charts', 'PV-vermogen', 'LAATSTE 24 UUR', 'pv-power', data.recent.pv_w,
      [{key: 'value', label: 'PV', color: COLORS.blue}], {mode: 'line', time: true, unit: 'W', digits: 0}],
    ['zon-charts', 'Opwek', 'LAATSTE 31 DAGEN', 'pv-days', days,
      [{key: 'pv_generated_kwh', quality: 'pv_quality', label: 'PV', color: COLORS.blue}], {mode: 'column', unit: 'kWh'}],
    ['zon-charts', 'Opwek', 'LAATSTE 13 VOLTOOIDE MAANDEN', 'pv-months', months,
      [{key: 'pv_generated_kwh', quality: 'pv_quality', label: 'PV', color: COLORS.blue}], {mode: 'column', month: true, unit: 'kWh'}],
  ];
  for (const container of ['stroom-charts', 'gas-charts', 'water-charts', 'zon-charts'])
    document.getElementById(container).replaceChildren();
  definitions.forEach(([container, title, eyebrow, id, rows, series, options]) => {
    panel(container, title, eyebrow, id);
    chart(id, title, rows, series, options);
  });
  const annual = [
    ['stroom-charts', 'Stroomafname', ['import_low_kwh', 'import_high_kwh'],
      'electricity_quality', lowHigh, 'kWh', 'import_total_kwh'],
    ['stroom-charts', 'Teruglevering', ['export_low_kwh', 'export_high_kwh'],
      'electricity_quality', exportLowHigh, 'kWh', 'export_total_kwh'],
    ['gas-charts', 'Gas', ['gas_m3'], 'gas_quality',
      [{key: 'gas_m3', quality: 'gas_quality', label: 'Gas', color: COLORS.red}], 'm³', 'gas_m3'],
    ['water-charts', 'Water', ['water_m3'], 'water_quality',
      [{key: 'water_m3', quality: 'water_quality', label: 'Water', color: COLORS.green}], 'm³', 'water_m3'],
    ['zon-charts', 'Opwek', ['pv_generated_kwh'], 'pv_quality',
      [{key: 'pv_generated_kwh', quality: 'pv_quality', label: 'PV', color: COLORS.blue}], 'kWh', 'pv_generated_kwh'],
  ];
  annual.forEach(([container, title, keys, quality, annualSeries, unit, compareKey]) => {
    const id = 'annual-' + container + '-' + compareKey;
    panel(container, title + ' per jaar', 'VIJF KALENDERJAREN · LOPEND JAAR T/M VORIGE MAAND', id);
    chart(id, title + ' per jaar', yearlyRows(years, keys, quality, data.today.slice(0, 7)),
      annualSeries, {mode: keys.length > 1 ? 'stacked' : 'column', year: true, unit});
    const comparison = monthlyComparison(years, compareKey, quality);
    const comparisonId = 'compare-' + container + '-' + compareKey;
    panel(container, title + ' per kalendermaand', 'VIJF JAAR · STAVEN EN GEMIDDELDE LIJN',
      comparisonId, true);
    chart(comparisonId, title + ' per kalendermaand', comparison.rows,
      comparison.series, {mode: 'compare', compare: true, unit});
  });
}

function makeCards(data) {
  const current = data.current;
  const cards = [
    ['Huisverbruik', current.home_w, 'W', current.p1_at_utc],
    ['Netafname', current.import_w, 'W', current.p1_at_utc],
    ['Zonnepanelen', current.pv_w, 'W', current.pv_at_utc],
    ['Gasstand', current.gas_meter_m3, 'm³', current.gas_at_utc],
    ['Waterdebiet', current.water_liters_per_min, 'l/min', current.water_at_utc],
  ];
  const host = document.getElementById('live-cards');
  host.replaceChildren();
  cards.forEach(([label, value, unit, stamp]) => {
    const card = document.createElement('div');
    card.className = 'card';
    const heading = document.createElement('span');
    heading.className = 'label';
    heading.textContent = label;
    const number = document.createElement('span');
    number.className = 'value' + (value == null ? ' missing' : '');
    number.textContent = value == null ? 'Geen actuele waarde' :
      `${valueText(value, unit === 'm³' ? 3 : 0)} ${unit}`;
    const detail = document.createElement('span');
    detail.className = 'detail';
    detail.textContent = localStamp(stamp);
    card.append(heading, number, detail);
    host.append(card);
  });
  const yesterday = data.days.at(-1);
  const costs = data.costs[yesterday.date] || {};
  const list = document.getElementById('cost-cards');
  list.replaceChildren();
  [['electricity', 'Stroom'], ['gas', 'Gas'], ['water', 'Water']].forEach(([key, label]) => {
    const row = document.createElement('div');
    row.className = 'cost-row';
    const name = document.createElement('span');
    name.textContent = label;
    const amount = document.createElement('strong');
    const cost = costs[key];
    if (!cost) {
      amount.textContent = 'Onbekend';
    } else {
      const network = cost.components.network;
      const fixed = cost.components.fixed_supply;
      const totalLabel = document.createElement('small');
      totalLabel.className = 'cost-total-label';
      totalLabel.textContent = key === 'water'
        ? fixed == null ? 'Totaal' : 'Totaal inclusief vastrecht'
        : network == null ? 'Totaal zonder netbeheer' : 'Totaal inclusief netbeheer';
      const total = document.createElement('span');
      total.textContent = euro(cost.total_eur);
      const note = document.createElement('small');
      const estimate = cost.status === 'estimate' || yesterday[key + '_quality'] === 'estimated'
        ? 'Schatting · ' : '';
      note.textContent = key === 'water'
        ? estimate + (fixed == null ? 'geen vastrechtregel' : 'waarvan vastrecht ' + euro(fixed))
        : network == null
          ? estimate + 'netbeheer ontbreekt in de kostenregels'
          : estimate + 'netbeheer ' + euro(network) +
            ' · overige kosten ' + euro(cost.excluding_network_eur);
      amount.append(totalLabel, total, note);
    }
    row.append(name, amount);
    list.append(row);
  });
}

async function refresh() {
  const message = document.getElementById('message');
  message.textContent = '';
  try {
    const response = await fetch('/api/dashboard', {cache: 'no-store'});
    if (!response.ok) throw Error('Server tijdelijk niet beschikbaar');
    const data = await response.json();
    document.querySelector('.nota-link').href =
      `${location.protocol}//${location.hostname}:${data.nota_port}/nota`;
    makeCards(data);
    makeCharts(data);
    document.getElementById('updated').textContent = 'Bijgewerkt ' + localStamp(data.generated_at_utc);
  } catch (error) {
    message.textContent = 'De gegevens konden niet worden geladen. Probeer Vernieuwen opnieuw.';
    console.error(error);
  }
}
document.querySelectorAll('.tabs button').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.tabs button').forEach(item =>
    item.classList.toggle('active', item === button));
  document.querySelectorAll('.tab-panel').forEach(panel =>
    panel.classList.toggle('active', panel.id === button.dataset.tab));
}));
document.getElementById('refresh').addEventListener('click', refresh);
refresh();
