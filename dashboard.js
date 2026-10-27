"use strict";

var MAX = 60;
var CIRC = 2 * Math.PI * 46;
var samples = { cpu: [], mem: [], disk: [] };
var polling = false;
var rings = {
  cpu: document.getElementById("cpu-ring"),
  mem: document.getElementById("mem-ring"),
  disk: document.getElementById("disk-ring")
};
Object.keys(rings).forEach(function (name) {
  rings[name].style.strokeDasharray = String(CIRC);
  rings[name].style.strokeDashoffset = String(CIRC);
});

function formatBytes(value) {
  if (value == null || isNaN(value)) return "—";
  var units = ["B", "KB", "MB", "GB", "TB"];
  var size = Number(value);
  var index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return (size >= 10 || index === 0 ? size.toFixed(0) : size.toFixed(1)) + " " + units[index];
}

function formatUptime(seconds) {
  seconds = Math.max(0, Math.floor(Number(seconds) || 0));
  var days = Math.floor(seconds / 86400);
  var hours = Math.floor((seconds % 86400) / 3600);
  var minutes = Math.floor((seconds % 3600) / 60);
  var secs = seconds % 60;
  if (days > 0) return days + "d " + hours + "h " + minutes + "m";
  return hours + "h " + minutes + "m " + secs + "s";
}

function push(list, value) {
  list.push(Number(value) || 0);
  if (list.length > MAX) list.shift();
}

function setGauge(name, value, color) {
  var percent = Math.max(0, Math.min(100, Number(value) || 0));
  var ring = rings[name];
  ring.style.strokeDashoffset = String(CIRC * (1 - percent / 100));
  ring.style.stroke = percent >= 90 ? "#ff5d5d" : color;
  document.getElementById(name + "-num").textContent = percent.toFixed(1);
}

function render(data) {
  if (!data || data.cpu_percent == null || !data.memory || !data.disk) {
    document.getElementById("error").textContent = (data && data.error) || "Collecting the first sample…";
    return;
  }
  document.getElementById("error").textContent = data.error || "";
  document.getElementById("host").textContent = data.hostname || "—";
  document.getElementById("uptime").textContent = formatUptime(data.uptime_seconds);
  document.getElementById("procs").textContent = data.process_count == null ? "—" : String(data.process_count);
  document.getElementById("load").textContent = data.load_avg
    ? data.load_avg.map(function (n) { return Number(n).toFixed(2); }).join("  ")
    : (data.cpu_count ? data.cpu_count + " cores" : "—");
  setGauge("cpu", data.cpu_percent, "#9dbe4a");
  setGauge("mem", data.memory.percent, "#e07a66");
  setGauge("disk", data.disk.percent, "#e0a45a");
  document.getElementById("cpu-detail").textContent = (data.system || "") + (data.cpu_count ? " · " + data.cpu_count + " cores" : "");
  document.getElementById("mem-detail").textContent = formatBytes(data.memory.used) + " / " + formatBytes(data.memory.total);
  document.getElementById("disk-detail").textContent = (data.disk.mount || "disk") + " · " + formatBytes(data.disk.free) + " free";
  push(samples.cpu, data.cpu_percent);
  push(samples.mem, data.memory.percent);
  push(samples.disk, data.disk.percent);
  drawChart();
}

function drawChart() {
  var canvas = document.getElementById("chart");
  var rect = canvas.getBoundingClientRect();
  var dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  var ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  var width = rect.width;
  var height = rect.height;
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = "rgba(237, 240, 230, 0.08)";
  ctx.lineWidth = 1;
  [0.25, 0.5, 0.75].forEach(function (mark) {
    var y = height * mark;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  });
  [
    [samples.cpu, "#9dbe4a"],
    [samples.mem, "#e07a66"],
    [samples.disk, "#e0a45a"]
  ].forEach(function (series) {
    var points = series[0];
    if (!points.length) return;
    ctx.beginPath();
    ctx.strokeStyle = series[1];
    ctx.lineWidth = 2;
    points.forEach(function (value, index) {
      var x = (index / (MAX - 1)) * width;
      var y = height - 8 - (Math.max(0, Math.min(100, value)) / 100) * (height - 16);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
}

function pollOnce() {
  return fetch("/api/stats", { cache: "no-store" })
    .then(function (response) { return response.json(); })
    .then(render)
    .catch(function () {
      document.getElementById("error").textContent = "No response from the sampler.";
    });
}

function startPolling() {
  if (polling) return;
  polling = true;
  document.getElementById("mode").textContent = "Polling /api/stats · 1s";
  pollOnce();
  setInterval(pollOnce, 1000);
}

function start() {
  if (!window.EventSource) {
    startPolling();
    return;
  }
  var source = new EventSource("/api/events");
  var saw = false;
  source.onmessage = function (event) {
    saw = true;
    document.getElementById("mode").textContent = "SSE /api/events · 1s";
    try {
      render(JSON.parse(event.data));
    } catch (err) {
      document.getElementById("error").textContent = "Bad event payload.";
    }
  };
  source.onerror = function () {
    if (saw) return;
    source.close();
    startPolling();
  };
  setTimeout(function () {
    if (!saw) {
      source.close();
      startPolling();
    }
  }, 2500);
}

window.addEventListener("resize", drawChart);
start();
