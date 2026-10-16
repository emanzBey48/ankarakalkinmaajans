/**
 * Tek seferlik Gemini kayıt aracı.
 *
 * Gunluk kota icin sadece 3 istek atar ve her birini
 * data/gemini-run-log.json icine ham istek/cevap + olcumlerle yazar.
 * Sitede gosterilen her AI cevabi bu kayittan gelir.
 *
 *   node tools/gemini-record.mjs
 *   node tools/gemini-record.mjs --force     # logu degistirip yeniden cagir
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SITE = path.resolve(HERE, '..');
const REPO = path.resolve(SITE, '..');
const DATA = path.join(SITE, 'data');
const LOG_FILE = path.join(DATA, 'gemini-run-log.json');
const MODEL = process.env.GEMINI_MODEL || 'gemini-3.8-flash';

function readKey() {
  if (process.env.GEMINI_API_KEY) return process.env.GEMINI_API_KEY.trim();
  const p = path.join(REPO, 'geminiapi.txt.txt');
  if (fs.existsSync(p)) return fs.readFileSync(p, 'utf8').trim();
  return '';
}

const key = readKey();
if (!key) {
  console.error('Gemini anahtari bulunamadi (GEMINI_API_KEY veya ../geminiapi.txt.txt).');
  process.exit(1);
}

const force = process.argv.includes('--force');
const retryFailed = process.argv.includes('--retry-failed');
if (fs.existsSync(LOG_FILE) && !force && !retryFailed) {
  console.log('Kayit zaten var. Yeniden uretmek icin: node tools/gemini-record.mjs --force');
  console.log('Sadece basarisiz cagrileri tekrar etmek icin: node tools/gemini-record.mjs --retry-failed');
  process.exit(0);
}

/* ------------------------------------------------------------------ prompts */

const pipelineFacts = `Proje: "Build the Builder".
Girdi: bir banka brief'i (Markdown + OpenAPI arac dosyasi).
Cikti: Freya platformunda calisan bir telefon ses agenti (workflow JSON).
Builder su asamalardan gecer:
1) brief_parser.py  -> brief metnindeki kurallari, tablolari, sonuc adlarini cikarir
2) openapi_parser.py -> arac (tool) semasini ve alanlarini okur
3) policy_spec.py   -> kurallari PolicySpec nesnesine normalize eder
4) date_calculator.py -> is gunleri, tatil, vade hesaplarini uretir
5) workflow_builder.py -> node'lar, prompt'lar, transition'lar, code step'ler
6) main.py           -> lokal yapısal kontrol + publish + pratik musteri testleri
Her asamada CIKTI dosyaya yazilir ve build log'a eklenir.`;

const PROMPT_N8N = `Sen bir n8n workflow uzmanısın. Aşağıdaki süreci n8n JSON export formatında, SADECE geçerli JSON olarak yaz.

${pipelineFacts}

Kurallar:
- Cevabin ilk karakteri { ve son karakteri } olsun. Markdown yok, aciklama yok.
- "nodes" her biri: name (benzersiz, Turkce karakter yok), type, typeVersion (sayi), position [x, y] (yatay, 260px aralikla), parameters (nesne).
- Sadece su gercek n8n node tiplerini kullan: n8n-nodes-base.manualTrigger, n8n-nodes-base.scheduleTrigger, n8n-nodes-base.readWriteFile, n8n-nodes-base.code, n8n-nodes-base.httpRequest, n8n-nodes-base.if, n8n-nodes-base.switch, n8n-nodes-base.merge, n8n-nodes-base.stopAndError, n8n-nodes-base.noOp.
- "connections" n8n formatinda olsun: { "NodeName": { "main": [ [ {"node": "Hedef", "type": "main", "index": 0} ] ] } }
- Bir node'un parametreleri kisa ve gercekci olsun (dosya yolu, url, js kodu ozeti).
- Toplam 9-12 node. Akis: tetikle -> brief oku -> arac semasi oku -> Gemini ile brief ozetle -> kod ile workflow JSON derle -> gecerlilik kontrolu (IF) -> hat varsa duzelt -> yayinla (HTTP) -> test kosulandir -> trace yaz -> bitir.
- Ayni node'a iki kez gitme, cevrim (cycle) kurma.`;

const PROMPT_SUMMARY = `Eski bir okul projesini anlatacak kisa bir metin yaz. Ton: net, profesyonel, biraz özgüvenli.

Proje: ${pipelineFacts}

Ayrıca: ajan "kredi kartı ekstode borcu" çağrılarını Türkçe yanıtlıyor, kimlik doğrulama, borç okuma, taksit/vade hesaplama, ödeme planı teklifi, e-posta ile beyan gönderme ve insan temsilcisine aktarma adımlarını içeriyor. Node sayısı 48, pratik müşteri sayısı 13.

Çıktı: 3 kısa paragraf + 4 maddelik madde işaretli liste. Sadece Türkçe. Markdown başlık kullanma.`;

const PROMPT_FAQ = `Bir okul sunumunda proje hakkında sorulan sorulara cevap verecek kısa bir bilgi tabanı yaz.

Proje: ${pipelineFacts}

JSON döndür, markdown yok. Şema:
{"faq":[{"q":"soru","kw":["anahtar1","anahtar2"],"a":"1-2 cevap cümlesi"}]}
8 soru yaz. Şunları kapsa: builder ne yapar, brief neden önemli, Gemini nerede kullanılıyor, n8n ne işe yarar, müşteri verisi kullanılıyor mu, ajan nasıl test ediliyor, kod el ile düzeltiliyor mu, projenin zor kısmı neydi.
Türkçe ve kısa olsun. Her "a" en fazla 2 cümle.`;

const calls = [
  { id: 'n8n-pipeline', title: 'n8n iş akışı üretimi', purpose: 'Builder pipeline’ını n8n JSON olarak tasarlat', prompt: PROMPT_N8N, json: true },
  { id: 'summary', title: 'Proje özeti', purpose: 'Sunum için Türkçe özet metni üret', prompt: PROMPT_SUMMARY, json: false },
  { id: 'faq', title: 'Bilgi tabanı (FAQ)', purpose: 'Asistanın offline cevapladığı 8 soruluk kayıt', prompt: PROMPT_FAQ, json: true },
];

/* --------------------------------------------------------------------- run */

function repairTruncated(text) {
  // Cevap token limitinde kesildiginde JSON yarim kaliyor: acik string/bracket'leri kapat.
  const stack = [];
  let inStr = false;
  let esc = false;
  for (const ch of text) {
    if (esc) { esc = false; continue; }
    if (ch === '\\') { if (inStr) esc = true; continue; }
    if (ch === '"') { inStr = !inStr; continue; }
    if (inStr) continue;
    if (ch === '{' || ch === '[') stack.push(ch);
    else if (ch === '}' || ch === ']') stack.pop();
  }
  let out = text;
  if (inStr) out += '"';
  out = out.replace(/,\s*$/, '');
  for (let i = stack.length - 1; i >= 0; i--) out += stack[i] === '{' ? '}' : ']';
  return out;
}

function parseJson(text) {
  const clean = String(text).trim().replace(/^```(?:json)?/i, '').replace(/```$/, '').trim();
  const start = clean.search(/[[{]/);
  const slice = clean.slice(start);
  const end = Math.max(slice.lastIndexOf('}'), slice.lastIndexOf(']'));
  try {
    return JSON.parse(end >= 0 ? slice.slice(0, end + 1) : slice);
  } catch (e) {
    return JSON.parse(repairTruncated(slice));
  }
}

const MODEL_FALLBACKS = [MODEL, 'gemini-3.6-flash', 'gemini-3.5-flash', 'gemini-flash-latest'];

async function post(model, body) {
  const res = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${encodeURIComponent(key)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return { res, raw: await res.text() };
}

async function runCall(call) {
  const startedAt = new Date();
  const t0 = Date.now();
  const body = {
    contents: [{ role: 'user', parts: [{ text: call.prompt }] }],
    generationConfig: {
      temperature: 0.2,
      maxOutputTokens: call.id === 'n8n-pipeline' ? 8192 : 3000,
      ...(call.json ? { responseMimeType: 'application/json' } : {}),
    },
    // Dusunme butcesi kapatilir: gecerli cikti (workflow JSON / ozet metni) kisalmasin.
    thinkingConfig: { thinkingBudget: 0 },
  };
  let res = null;
  let raw = '';
  let usedModel = MODEL;
  let attempts = [];
  for (const model of MODEL_FALLBACKS) {
    for (let tryNo = 1; tryNo <= 3; tryNo++) {
      const r = await post(model, body);
      attempts.push(`${model}#${tryNo}:${r.res.status}`);
      if (r.res.ok) { res = r.res; raw = r.raw; usedModel = model; break; }
      if (r.res.status === 429 || r.res.status === 503) {
        await new Promise((s) => setTimeout(s, 4000 * tryNo));
        continue;
      }
      break; // 400/404 gibi kalici hata: sonraki modele gec
    }
    if (res) break;
  }
  const ms = Date.now() - t0;
  if (!res) throw new Error(`tüm modeller başarısız (${attempts.join(' ')})`);
  const json = JSON.parse(raw);
  const candidate = json.candidates?.[0] || {};
  const text = (candidate.content?.parts || []).map((p) => p.text || '').join('');
  let parsed = null;
  let parseError = null;
  if (call.json) {
    try { parsed = parseJson(text); } catch (e) { parseError = String(e.message || e); }
  }
  return {
    id: call.id,
    title: call.title,
    purpose: call.purpose,
    model: usedModel,
    provider: 'Google Gemini API (generativelanguage.googleapis.com)',
    endpoint: `${usedModel}:generateContent`,
    attempts,
    startedAt: startedAt.toISOString(),
    durationMs: ms,
    usage: json.usageMetadata || {},
    finishReason: candidate.finishReason || null,
    promptChars: call.prompt.length,
    prompt: call.prompt,
    responseText: text,
    responseJson: parsed,
    parseError,
    ok: !parseError,
  };
}

const log = {
  generatedAt: new Date().toISOString(),
  model: MODEL,
  note: 'Bu koşu demo sunumu için bir kez gerçek Gemini API ile yapıldı ve loglandı. Site bu kayıtları okur, API tekrar çağırmaz.',
  calls: [],
};

let pending = calls;
if (retryFailed && fs.existsSync(LOG_FILE)) {
  const prev = JSON.parse(fs.readFileSync(LOG_FILE, 'utf8'));
  const done = new Set(prev.calls.filter((c) => c.ok).map((c) => c.id));
  pending = calls.filter((c) => !done.has(c.id));
  log.calls.push(...prev.calls.filter((c) => c.ok));
  console.log(`Basarili cagri korunuyor (${log.calls.length}), yeniden denen: ${pending.length}\n`);
}

fs.mkdirSync(DATA, { recursive: true });

for (const call of pending) {
  process.stdout.write(`- ${call.title} ... `);
  try {
    const r = await runCall(call);
    log.calls.push(r);
    console.log(`ok (${r.durationMs}ms, ${r.usage.totalTokenCount || '?'} token${r.parseError ? ', JSON PARSE FAIL' : ''})`);
    if (r.parseError) console.warn('  ! ' + r.parseError + '\n' + r.responseText.slice(-300));
  } catch (err) {
    log.calls.push({ id: call.id, title: call.title, purpose: call.purpose, ok: false, error: String(err.message || err) });
    console.log('HATA: ' + err.message);
  }
  await new Promise((r) => setTimeout(r, 1200));
}

fs.writeFileSync(LOG_FILE, JSON.stringify(log, null, 2), 'utf8');
console.log('\nYazildi: ' + LOG_FILE);
console.log('Basarili cagri: ' + log.calls.filter((c) => c.ok).length + '/' + log.calls.length);
