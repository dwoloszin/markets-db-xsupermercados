# Fontes de Códigos de Barras - Análise Detalhada

## 🏆 RANKING DAS MELHORES FONTES

### 1. ⭐⭐⭐⭐⭐ Cosmos (Bluesoft) - A MELHOR
**URL:** https://cosmos.bluesoft.com.br

| Característica | Detalhe |
|----------------|---------|
| **Cobertura** | +8 milhões de produtos brasileiros |
| **Dados** | GTIN, Nome, NCM, Marca, Fabricante, Preço Médio, Foto |
| **API** | Tem API paga (cosmos.bluesoft.io) |
| **Scraping** | Possível, mas com proteção |
| **Bloqueio** | Moderado (rate limit + CAPTCHA após muitos requests) |

**Como funciona:**
- Busca por termo: `cosmos.bluesoft.com.br/pesquisa?q=coca-cola`
- Busca por código: `cosmos.bluesoft.com.br/produtos/7894900011517`
- Busca por NCM: `cosmos.bluesoft.com.br/ncms/2202.10.00`
- Busca por fabricante: `cosmos.bluesoft.com.br/fabricantes/19168-ambev`

**Proteções:**
- Rate limit: ~100 requests/minuto
- CAPTCHA após muitos requests
- Bloqueia IPs que fazem muitas requisições

**Como evitar bloqueio:**
```python
# Delays longos (5-15 segundos entre requests)
# Rotação de User-Agent
# Proxies residenciais (não datacenter)
# Simular comportamento humano (scroll, cliques)
```

---

### 2. ⭐⭐⭐⭐⭐ Open Food Facts - EXCELENTE
**URL:** https://br.openfoodfacts.org

| Característica | Detalhe |
|----------------|---------|
| **Cobertura** | +3 milhões de produtos (mundial), ~200k BR |
| **Dados** | GTIN, Nome, Marca, Ingredientes, Nutrição, Foto |
| **API** | GRATUITA e aberta! |
| **Scraping** | Desnecessário (API completa) |
| **Bloqueio** | Baixo (API generosa) |

**API Gratuita:**
```bash
# Busca por código
curl "https://world.openfoodfacts.org/api/v2/product/7894900011517.json"

# Busca por termo
curl "https://world.openfoodfacts.org/cgi/search.pl?search_terms=coca-cola&json=1"

# Produtos do Brasil
curl "https://world.openfoodfacts.org/country/brazil.json"

# Por categoria
curl "https://world.openfoodfacts.org/category/beverages.json"
```

**Vantagem:** Não precisa se preocupar com bloqueio!

---

### 3. ⭐⭐⭐⭐ Mercado Livre - MUITO BOM
**URL:** https://api.mercadolibre.com

| Característica | Detalhe |
|----------------|---------|
| **Cobertura** | Milhões de produtos (varejo) |
| **Dados** | GTIN, Nome, Marca, Preço, Fotos |
| **API** | GRATUITA e pública |
| **Scraping** | Desnecessário (API completa) |
| **Bloqueio** | Baixo (API oficial) |

**API Gratuita:**
```bash
# Busca por termo
curl "https://api.mercadolibre.com/sites/MLB/search?q=coca-cola"

# Detalhes do produto (tem GTIN)
curl "https://api.mercadolibre.com/items/MLB1234567"

# Categorias úteis:
# MLB1403 - Alimentos e Bebidas
# MLB1246 - Beleza e Cuidado Pessoal
# MLB1144 - Limpeza
```

**Atenção:** Nem todos os produtos têm GTIN cadastrado (~60% têm)

---

### 4. ⭐⭐⭐⭐ GS1 Brasil - OFICIAL
**URL:** https://www.gs1br.org

| Característica | Detalhe |
|----------------|---------|
| **Cobertura** | +550 milhões (mundial via Verified by GS1) |
| **Dados** | GTIN, Nome, Marca (oficial do fabricante) |
| **API** | Paga (para associados) |
| **Scraping** | Difícil (proteção forte) |
| **Bloqueio** | Alto |

**Não recomendo scraping** - é o órgão oficial e tem proteção forte.
Melhor usar para validar dados obtidos de outras fontes.

---

### 5. ⭐⭐⭐ UPCitemdb - BOM
**URL:** https://www.upcitemdb.com

| Característica | Detalhe |
|----------------|---------|
| **Cobertura** | +500 milhões de produtos (internacional) |
| **Dados** | GTIN, Nome, Marca, Descrição |
| **API** | Tem (limite gratuito) |
| **Scraping** | Possível |
| **Bloqueio** | Moderado |

```bash
# Busca por código
curl "https://api.upcitemdb.com/prod/trial/lookup?upc=7894900011517"
```

---

### 6. ⭐⭐⭐ Barcode Lookup - BOM
**URL:** https://www.barcodelookup.com

| Característica | Detalhe |
|----------------|---------|
| **Cobertura** | +500 milhões de produtos |
| **Dados** | GTIN, Nome, Marca, Categoria |
| **API** | Paga |
| **Scraping** | Possível com cuidado |
| **Bloqueio** | Moderado |

---

### 7. ⭐⭐⭐ EAN-Search - BOM
**URL:** https://www.ean-search.org

| Característica | Detalhe |
|----------------|---------|
| **Cobertura** | +200 milhões (foco Europa) |
| **Dados** | GTIN, Nome, Categoria |
| **API** | Paga |
| **Scraping** | Possível |
| **Bloqueio** | Baixo |

---

## 🏭 CATÁLOGOS DE FABRICANTES

### Ambev
- **Cosmos:** https://cosmos.bluesoft.com.br/fabricantes/19168-ambev
- **Marcas:** Brahma, Skol, Antarctica, Budweiser, Stella Artois, Corona, Guaraná Antarctica
- **GTINs começam com:** 789 (Brasil)

### Coca-Cola (Solar BR)
- **Cosmos:** https://cosmos.bluesoft.com.br/fabricantes/1182-solar-br
- **Marcas:** Coca-Cola, Fanta, Sprite, Del Valle, Schweppes, Crystal
- **GTINs começam com:** 7894900

### Nestlé
- **Cosmos:** https://cosmos.bluesoft.com.br/fabricantes/1-nestle
- **Marcas:** Nescafé, Ninho, Maggi, Kitkat, Nesfit
- **GTINs começam com:** 7891000

### BRF (Sadia/Perdigão)
- **Cosmos:** https://cosmos.bluesoft.com.br/fabricantes/2-brf
- **Marcas:** Sadia, Perdigão, Qualy
- **GTINs começam com:** 7893000 (Sadia), 7891515 (Perdigão)

### Unilever
- **Cosmos:** https://cosmos.bluesoft.com.br/fabricantes/3-unilever
- **Marcas:** Omo, Comfort, Dove, Hellmann's, Knorr
- **GTINs começam com:** 7891150

### P&G
- **Marcas:** Ariel, Downy, Gillette, Pampers, Oral-B

### JBS/Friboi
- **Cosmos:** https://cosmos.bluesoft.com.br/fabricantes/jbs
- **Marcas:** Friboi, Seara, Swift

---

## 🛒 SUPERMERCADOS/ATACADISTAS (Difícil scraping)

| Site | Tem EAN? | API? | Dificuldade |
|------|----------|------|-------------|
| Carrefour | Sim (oculto) | Não | 🔴 Alta (login) |
| Pão de Açúcar | Sim (oculto) | Não | 🔴 Alta (login) |
| Assaí | Parcial | Não | 🔴 Alta |
| Makro | Parcial | Não | 🔴 Alta (B2B) |
| Atacadão | Não | Não | 🔴 Impossível |
| Amazon BR | Sim (ASIN) | Parcial | 🟡 Média |

**Não recomendo** tentar scraping de supermercados - proteção muito forte e poucos GTINs expostos.

---

## 🚫 TÉCNICAS ANTI-BLOQUEIO

### 1. Rate Limiting Inteligente

```python
# ❌ ERRADO: Muitos requests rápidos
for produto in produtos:
    requests.get(url)  # Bloqueio em minutos!

# ✅ CORRETO: Delays variáveis
import random
import time

for produto in produtos:
    requests.get(url)
    delay = random.uniform(3, 10)  # 3-10 segundos
    time.sleep(delay)
```

### 2. Rotação de User-Agent

```python
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)",
    "Mozilla/5.0 (Android 13; Mobile; rv:109.0) Gecko/109.0",
]

headers = {"User-Agent": random.choice(USER_AGENTS)}
```

### 3. Rotação de Proxies

```python
# Proxies gratuitos (instáveis)
PROXIES = [
    "http://proxy1.example.com:8080",
    "http://proxy2.example.com:8080",
]

# Proxies residenciais (pagos, melhores)
# - Bright Data (brightdata.com)
# - Smartproxy (smartproxy.com)
# - Oxylabs (oxylabs.io)
```

### 4. Simular Comportamento Humano

```python
# Variar padrões de acesso
# - Não acessar sempre na mesma ordem
# - Fazer pausas longas periodicamente
# - Acessar páginas diferentes (não só produtos)
# - Variar horários de execução
```

### 5. Respeitar robots.txt

```
# Verificar: https://cosmos.bluesoft.com.br/robots.txt
# Alguns sites bloqueiam scrapers que ignoram robots.txt
```

### 6. Detectar e Reagir a Bloqueios

```python
def fazer_request(url):
    response = requests.get(url)

    # Detectar bloqueio
    if response.status_code == 429:  # Too Many Requests
        print("Rate limit! Aguardando 5 minutos...")
        time.sleep(300)
        return fazer_request(url)

    if response.status_code == 403:  # Forbidden
        print("IP bloqueado! Trocando proxy...")
        trocar_proxy()
        return fazer_request(url)

    if "captcha" in response.text.lower():
        print("CAPTCHA detectado! Pausando 10 minutos...")
        time.sleep(600)
        return fazer_request(url)

    return response
```

---

## 📊 ESTRATÉGIA RECOMENDADA

### Fase 1: Fontes sem bloqueio (PRIORIDADE)
1. **Open Food Facts** - API gratuita, sem limite
2. **Mercado Livre** - API pública

### Fase 2: Fontes com proteção moderada
3. **Cosmos** - Com delays longos (5-15s)
4. **UPCitemdb** - Com delays moderados

### Fase 3: Fontes complementares
5. **EAN-Search**
6. **Barcode Lookup**

### Estimativa de coleta (24h rodando):

| Fonte | Produtos/dia | Com delays |
|-------|-------------|------------|
| Open Food Facts | 50.000+ | Sem limite |
| Mercado Livre | 10.000+ | ~2s delay |
| Cosmos | 5.000-10.000 | 5-15s delay |
| UPCitemdb | 3.000-5.000 | 3-5s delay |
| **TOTAL** | **~70.000/dia** | |

---

## 🔗 FONTES

- [GS1 Brasil](https://gs1br.org/) - Órgão oficial de códigos de barras
- [Cosmos Bluesoft](https://cosmos.bluesoft.com.br/) - Maior catálogo BR
- [Open Food Facts](https://br.openfoodfacts.org/) - Base aberta de alimentos
- [Mercado Livre API](https://developers.mercadolivre.com.br/) - Documentação API
- [ScrapingBee - Anti-blocking](https://www.scrapingbee.com/blog/web-scraping-without-getting-blocked/) - Técnicas anti-bloqueio
- [ZenRows - Rate Limiting](https://www.zenrows.com/blog/web-scraping-rate-limit/) - Como evitar rate limit
