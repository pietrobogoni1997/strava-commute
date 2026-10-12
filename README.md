# Strava Commute Analyzer

App web locale per analizzare i tuoi percorsi commute su Strava.

## Funzionalità

- **Visualizzazione percorsi** su mappa OpenStreetMap
- **Tempo medio A→B** — seleziona due punti sulla mappa e calcola media, mediana, min, max del tempo impiegato
- **Analisi soste** — vede dove ti fermi ai semafori, e heatmap della velocità lungo il percorso
- **Statistiche globali** — medie per tipo di attività, km totali, grafico mensile

---

## Setup (una volta sola)

### 1. Registra la tua app su Strava

1. Vai su **https://www.strava.com/settings/api** (accedi con il tuo account Strava)
2. Compila il form:
   - **Application Name**: qualcosa tipo `Commutanalyzer`
   - **Category**: Other
   - **Club**: lascia vuoto
   - **Website**: `http://localhost`
   - **Authorization Callback Domain**: `localhost`
3. Clicca **Create**
4. Copia **Client ID** e **Client Secret** dalla pagina successiva

### 2. Configura le credenziali

```bash
cd strava-commute
cp .env.example .env
```

Apri `.env` con un editor di testo e incolla i tuoi valori:

```
STRAVA_CLIENT_ID=123456
STRAVA_CLIENT_SECRET=abcdef1234567890abcdef1234567890abcdef12
SECRET_KEY=qualcosa-di-casuale-e-lungo
```

### 3. Installa le dipendenze Python

Richiede **Python 3.9+**

```bash
pip install -r requirements.txt
```

*(Su Mac con Python da Homebrew: `pip3 install -r requirements.txt`)*

### 4. Avvia l'app

```bash
python app.py
```

Apri il browser su **http://localhost:5000** e clicca **Accedi con Strava**.

---

## Come si usa

### Percorsi
- Alla prima apertura l'app scarica tutte le tue attività (può richiedere qualche minuto)
- Spunta **Solo commute** per vedere solo le attività marcate come commute su Strava
- Seleziona le attività dalla lista (o usa "Seleziona tutte") per evidenziarle sulla mappa
- Clicca **↻ Aggiorna** per scaricare nuove attività

### Tempo A→B
1. Clicca sul tab **⏱ Tempo A→B**
2. Clicca **📍 Punto A** poi clicca sulla mappa nel punto di partenza (es. casa)
3. Clicca **📍 Punto B** poi clicca nel punto di arrivo (es. ufficio)
4. Regola il **raggio** (metri) — quanto lontano dal punto conta come "passaggio"
5. Clicca **Analizza**

Otterrai: tempo medio, mediana, migliore, peggiore, numero di campioni.

### Soste e velocità
1. Clicca su un'attività dalla lista per selezionarla
2. Vai sul tab **🛑 Soste**
3. Regola la **soglia velocità** (es. 2 km/h = fermi al semaforo)
4. Clicca **Analizza soste**

La mappa mostrerà:
- **Heatmap velocità** — verde = veloce, rosso = lento
- **Punti rossi** = soste, più grande = sosta più lunga

### Statistiche
- Tab **📊 Statistiche** → **Carica statistiche commute**
- Vedi medie per tipo di mezzo, km totali, e grafico delle attività mese per mese

---

## Note tecniche

- Le attività vengono **messe in cache** localmente nel file `activities_cache.json` per non ri-scaricarle ogni volta
- Il token Strava viene **rinnovato automaticamente** alla scadenza (ogni 6 ore)
- L'app non invia dati a nessun server esterno, gira solo in locale
- Le API Strava hanno un limite di 100 richieste/15 min — se hai molte attività da analizzare con gli stream GPS, potrebbe volerci del tempo

## Struttura file

```
strava-commute/
├── app.py                 # Backend Flask
├── requirements.txt       # Dipendenze Python
├── .env                   # Credenziali (da creare tu, non committare!)
├── .env.example           # Template
├── activities_cache.json  # Cache (creato automaticamente)
├── templates/
│   ├── index.html         # Pagina login
│   └── dashboard.html     # Dashboard principale
└── static/
    └── css/
        └── style.css      # Stili
```
