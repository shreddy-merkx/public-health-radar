// Public Health Radar – Handy-App (läuft komplett im Browser)
"use strict";

const $ = s => document.querySelector(s);
const SEITE = 60;

// ---------- kleiner, sicherer Speicher (nur auf diesem Gerät) ----------
const speicher = {
  lies(k, std) { try { const v = localStorage.getItem("phr_" + k); return v ? JSON.parse(v) : std; } catch { return std; } },
  schreib(k, v) { try { localStorage.setItem("phr_" + k, JSON.stringify(v)); } catch { /* egal */ } }
};

const zustand = {
  daten: null,
  tab: speicher.lies("tab", "top"),
  sprache: speicher.lies("sprache", "alle"),
  zeit: speicher.lies("zeit", 7),
  sort: "p",
  suche: "",
  anzahl: SEITE,
  gelesen: new Set(speicher.lies("gelesen", [])),
  merk: speicher.lies("merk", {}),
  neuSeit: speicher.lies("letzterBesuch", null),
};

function id(e) { // kurzer Fingerabdruck der URL
  let h = 0; const s = e.u;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

// ---------- Zeit hübsch anzeigen ----------
function relativ(iso) {
  const min = Math.round((Date.now() - new Date(iso)) / 60000);
  if (min < 2) return "gerade eben";
  if (min < 60) return `vor ${min} Min.`;
  const h = Math.round(min / 60);
  if (h < 24) return `vor ${h} Std.`;
  const t = Math.round(h / 24);
  return t === 1 ? "gestern" : `vor ${t} Tagen`;
}
function datumLang(iso) {
  return new Date(iso).toLocaleString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

// ---------- Daten laden ----------
async function laden() {
  try {
    const r = await fetch("data.json?t=" + Date.now(), { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    zustand.daten = await r.json();
    zustand.geladenUm = Date.now();
  } catch (err) {
    try { const r = await fetch("data.json"); zustand.daten = await r.json(); } catch { /* offline & nichts gespeichert */ }
  }
  if (!zustand.daten) {
    $("#stand").textContent = "Noch keine Daten";
    $("#liste").innerHTML = `<div class="leer"><b>Noch keine Nachrichten da</b>Der Sammel-Server ist noch nicht gelaufen. Schau in ein paar Minuten wieder rein.</div>`;
    return;
  }
  const d = zustand.daten;
  $("#stand").textContent = `Stand ${datumLang(d.stand)} · ${d.statistik.webseiten} Quellen`;
  zeichneTabs();
  zeichneListe();
}

// ---------- Filtern ----------
function gefiltert({ ohneTab = false } = {}) {
  const d = zustand.daten;
  let liste = zustand.tab === "merk" && !ohneTab ? Object.values(zustand.merk) : d.eintraege;
  const grenze = Date.now() - zustand.zeit * 864e5;
  if (zustand.sprache !== "alle") liste = liste.filter(e => e.l === zustand.sprache);
  if (zustand.tab !== "merk" || ohneTab) liste = liste.filter(e => new Date(e.d) >= grenze);
  if (zustand.suche) {
    const w = zustand.suche.toLowerCase().split(/\s+/).filter(Boolean);
    liste = liste.filter(e => {
      const t = (e.t + " " + e.q + " " + (e.x || "") + " " + e.o.map(o => o[0] + " " + o[2]).join(" ")).toLowerCase();
      return w.every(x => t.includes(x));
    });
  }
  return liste;
}

function fuerTab(liste) {
  const t = zustand.tab;
  if (t === "top") return liste.slice().sort((a, b) => b.p - a.p).slice(0, 50);
  if (t === "alle" || t === "merk") return liste;
  return liste.filter(e => e.k.includes(t));
}

// ---------- Tabs ----------
function zeichneTabs() {
  const d = zustand.daten;
  const basis = gefiltert({ ohneTab: true });
  const zahl = {};
  basis.forEach(e => e.k.forEach(k => zahl[k] = (zahl[k] || 0) + 1));
  const tabs = [{ id: "top", name: "Top 50", icon: "🔥" }, { id: "alle", name: "Alle", icon: "", n: basis.length }]
    .concat(d.kategorien.filter(k => zahl[k.id]).map(k => ({ ...k, n: zahl[k.id] })))
    .concat([{ id: "merk", name: "Merkliste", icon: "🔖", n: Object.keys(zustand.merk).length }]);
  if (!tabs.some(t => t.id === zustand.tab)) zustand.tab = "top";
  const nav = $("#tabs");
  nav.innerHTML = "";
  tabs.forEach(t => {
    const b = document.createElement("button");
    b.className = "tab" + (t.id === zustand.tab ? " an" : "");
    b.innerHTML = `${t.icon ? t.icon + " " : ""}${t.name}${t.n != null ? `<span class="zahl">${t.n}</span>` : ""}`;
    b.onclick = () => { zustand.tab = t.id; zustand.anzahl = SEITE; speicher.schreib("tab", t.id); zeichneTabs(); zeichneListe(); window.scrollTo({ top: 0 }); };
    nav.appendChild(b);
  });
  const aktiv = nav.querySelector(".an");
  if (aktiv) aktiv.scrollIntoView({ block: "nearest", inline: "center" });
}

// ---------- Liste ----------
function zeichneListe() {
  const d = zustand.daten;
  const kats = Object.fromEntries(d.kategorien.map(k => [k.id, k]));
  let liste = fuerTab(gefiltert());
  if (zustand.sort === "d") liste = liste.slice().sort((a, b) => new Date(b.d) - new Date(a.d));
  else if (zustand.tab !== "top") liste = liste.slice().sort((a, b) => b.p - a.p);

  const main = $("#liste");
  main.innerHTML = "";
  if (!liste.length) {
    main.innerHTML = zustand.tab === "merk"
      ? `<div class="leer"><b>Merkliste ist leer</b>Tippe bei einer Meldung auf das Lesezeichen, um sie hier zu speichern.</div>`
      : `<div class="leer"><b>Keine Meldungen</b>Versuche einen längeren Zeitraum oder eine andere Sprache.</div>`;
    return;
  }
  const vorlage = $("#karte-vorlage");
  const frag = document.createDocumentFragment();
  let abschnitt = null;
  liste.slice(0, zustand.anzahl).forEach(e => {
    if (zustand.tab === "top" && zustand.sort === "p") {
      const a = e.s >= 4 ? "Besonders wichtig" : "Ebenfalls wichtig";
      if (a !== abschnitt) { abschnitt = a; const h = document.createElement("div"); h.className = "abschnitt"; h.textContent = a; frag.appendChild(h); }
    }
    const k = vorlage.content.firstElementChild.cloneNode(true);
    const eid = id(e);
    k.dataset.s = e.s;
    if (zustand.gelesen.has(eid)) k.classList.add("gelesen");
    const titel = k.querySelector(".titel");
    titel.textContent = e.t;
    titel.href = e.u;
    titel.addEventListener("click", () => markiereGelesen(eid, k));
    const oben = k.querySelector(".kats");
    const wichtig = e.s >= 4 ? `<span class="wichtig">${"●".repeat(e.s)}</span> ` : "";
    oben.innerHTML = wichtig + e.k.slice(0, 2).map(x => kats[x] ? `<span class="kat">${kats[x].icon} ${kats[x].name}</span>` : "").join(" · ");
    if (zustand.neuSeit && e.e > zustand.neuSeit) k.querySelector(".neu").hidden = false;
    if (e.x) { const p = k.querySelector(".auszug"); p.textContent = e.x; p.hidden = false; }
    k.querySelector(".quelle").textContent = e.q;
    const zeit = k.querySelector("time");
    zeit.textContent = relativ(e.d); zeit.dateTime = e.d; zeit.title = datumLang(e.d);
    if (e.o && e.o.length) {
      const btn = k.querySelector(".mehr-quellen");
      const weitere = e.n - 1;
      btn.textContent = `+${weitere} ${weitere === 1 ? "Quelle" : "Quellen"}`;
      btn.hidden = false;
      const ul = k.querySelector(".andere");
      btn.onclick = () => {
        if (!ul.childElementCount) {
          e.o.forEach(([q, u, t]) => {
            const li = document.createElement("li");
            const a = document.createElement("a");
            a.href = u; a.target = "_blank"; a.rel = "noopener";
            a.innerHTML = `<b></b>${t ? ` <span class="alt"></span>` : ""}`;
            a.querySelector("b").textContent = q;
            if (t) a.querySelector(".alt").textContent = "– " + t;
            li.appendChild(a); ul.appendChild(li);
          });
          if (e.n - 1 > e.o.length) { const li = document.createElement("li"); li.className = "alt"; li.textContent = `… und ${e.n - 1 - e.o.length} weitere`; ul.appendChild(li); }
        }
        ul.hidden = !ul.hidden;
      };
    }
    const merk = k.querySelector(".merken");
    if (zustand.merk[eid]) merk.classList.add("an");
    merk.onclick = () => {
      if (zustand.merk[eid]) { delete zustand.merk[eid]; merk.classList.remove("an"); }
      else { zustand.merk[eid] = e; merk.classList.add("an"); }
      speicher.schreib("merk", zustand.merk);
      zeichneTabs();
      if (zustand.tab === "merk") zeichneListe();
    };
    frag.appendChild(k);
  });
  main.appendChild(frag);
  if (liste.length > zustand.anzahl) {
    const b = document.createElement("button");
    b.className = "mehr-laden";
    b.textContent = `Weitere ${Math.min(SEITE, liste.length - zustand.anzahl)} von ${liste.length - zustand.anzahl} anzeigen`;
    b.onclick = () => { zustand.anzahl += SEITE; zeichneListe(); };
    main.appendChild(b);
  }
}

function markiereGelesen(eid, karte) {
  zustand.gelesen.add(eid);
  karte.classList.add("gelesen");
  speicher.schreib("gelesen", [...zustand.gelesen].slice(-3000));
}

// ---------- Bedienelemente ----------
function segment(sel, schluessel, umwandeln = v => v) {
  const el = $(sel);
  el.querySelectorAll("button").forEach(b => {
    b.classList.toggle("an", String(zustand[schluessel]) === b.dataset.v);
    b.onclick = () => {
      zustand[schluessel] = umwandeln(b.dataset.v);
      zustand.anzahl = SEITE;
      if (schluessel !== "sort") speicher.schreib(schluessel, zustand[schluessel]);
      el.querySelectorAll("button").forEach(x => x.classList.toggle("an", x === b));
      zeichneTabs(); zeichneListe();
    };
  });
}
segment("#f-sprache", "sprache");
segment("#f-zeit", "zeit", Number);
segment("#f-sort", "sort");

let suchTimer;
$("#btn-suche").onclick = () => {
  const leiste = $("#suchleiste");
  leiste.hidden = !leiste.hidden;
  $("#btn-suche").classList.toggle("an", !leiste.hidden);
  if (!leiste.hidden) $("#suchfeld").focus();
  else { $("#suchfeld").value = ""; zustand.suche = ""; zeichneTabs(); zeichneListe(); }
};
$("#suchfeld").addEventListener("input", ev => {
  clearTimeout(suchTimer);
  suchTimer = setTimeout(() => { zustand.suche = ev.target.value.trim(); zustand.anzahl = SEITE; zeichneTabs(); zeichneListe(); }, 200);
});

$("#btn-info").onclick = async () => {
  $("#info").hidden = false;
  document.body.style.overflow = "hidden";
  const d = zustand.daten;
  if (d) {
    const s = d.statistik;
    $("#statistik").innerHTML = [
      [s.webseiten, "verschiedene Quellen mit Treffern"], [s.abrufe_ok + " / " + s.abrufe, "Abrufe erfolgreich"],
      [s.meldungen, "Meldungen gefunden"], [s.themen, "Themen nach Bündelung"]]
      .map(([z, t]) => `<div><b>${Number.isFinite(z) ? z.toLocaleString("de-DE") : z}</b><span>${t}</span></div>`).join("");
  }
  try {
    const r = await fetch("status.json?t=" + Date.now());
    const st = await r.json();
    const kaputt = st.abrufe.filter(a => !a.ok || a.gesamt === 0);
    $("#fehlerliste").innerHTML = kaputt.length
      ? kaputt.map(a => `<div><b></b> <span></span></div>`).join("")
      : "Alle Quellen haben geantwortet. 👍";
    kaputt.forEach((a, i) => {
      const zeile = $("#fehlerliste").children[i];
      zeile.querySelector("b").textContent = a.quelle + (a.anbieter !== a.quelle ? ` (${a.anbieter})` : "");
      zeile.querySelector("span").textContent = a.ok ? "– liefert keine Einträge" : "– " + a.fehler;
    });
  } catch { $("#fehlerliste").textContent = "Status nicht verfügbar."; }
};
function infoZu() { $("#info").hidden = true; document.body.style.overflow = ""; }
$("#info-zu").onclick = infoZu;
$("#info").addEventListener("click", ev => { if (ev.target.id === "info") infoZu(); });
$("#btn-gelesen-reset").onclick = () => { zustand.gelesen.clear(); speicher.schreib("gelesen", []); zeichneListe(); infoZu(); };

// "NEU"-Markierung: alles, was seit dem letzten Besuch dazugekommen ist
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") speicher.schreib("letzterBesuch", new Date().toISOString().slice(0, 16) + "Z");
  else if (zustand.geladenUm && Date.now() - zustand.geladenUm > 30 * 60000) { zustand.neuSeit = speicher.lies("letzterBesuch", null); laden(); }
});

if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
laden();
