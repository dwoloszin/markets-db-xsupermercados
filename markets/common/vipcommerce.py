"""
vipcommerce.py - Shared client for VipCommerce storefronts (Rossi, Davo).

VipCommerce (services.vipcommerce.com.br) is a multi-tenant grocery platform.
Everything the Angular storefront shows comes from a JSON API that is public
once you have a "loja" JWT. Reverse engineered by intercepting the site:

  GET  /api-admin/v1/organizacoes/filiais/dominio/{domain}
        -> {"data": {"id": <filial>, "organizacao": {"id": <org>}}}
  POST /api-admin/v1/org/{org}/auth/loja/login   (headers DomainKey, OrganizationId)
        body {"domain": "<domain>", "username": "loja", "key": "<shared key>"}
        -> {"data": "<JWT>"}         (the key is the same for every tenant we checked)
  GET  /api-admin/v1/org/{org}/loja/centros_distribuicoes/1               -> delivery hub info
  GET  /api-admin/v1/org/{org}/filial/1/loja/tipo_entregas/realiza_entrega?cep=  -> served?
  GET  /api-admin/v1/org/{org}/filial/1/centro_distribuicao/1/loja/classificacoes_mercadologicas/departamentos/arvore
  GET  .../departamentos/{id}/produtos?page=N   -> 20/page, paginator.total_pages
        product: produto_id, descricao, codigo_barras (EAN), preco, preco_original,
                 oferta{preco_oferta, quantidade_minima, nome, tag}, marca, imagem, link,
                 unidade_sigla, disponivel, quantidade_maxima

Pickup distribution centres (centros_distribuicoes/retiradas) expose an EMPTY
catalogue, so CD 1 (delivery) is used for every ZIP.
Barcode: inline codigo_barras (~92%).
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from markets.common.geo import format_zip, normalize_zip, to_float
from markets.common.gtin import first_gtin
from markets.common.http import get_json, make_session, post_json
from markets.common.offer import make_offer

API_ROOT = "https://services.vipcommerce.com.br/api-admin/v1"
IMAGE_BASE = "https://produto-assets-vipcommerce-com-br.br-se1.magaluobjects.com/250x250"
# Shared storefront login key observed on rossidelivery.com.br and davo.com.br (Sep 2026).
DEFAULT_LOGIN_KEY = "df072f85df9bf7dd71b6811c34bdbaa4f219d98775b56cff9dfa5f8ca1bf8469"
FILIAL = "1"
DELIVERY_CD = "1"


class VipCommerceMarket:
    def __init__(self, *, store_key: str, domain: str, web_base: str, org_id: Optional[int] = None):
        self.key = store_key
        self.domain = domain
        self.web = web_base.rstrip("/")
        self.org_id = org_id
        self.log = f"[{store_key}] "
        self.session = make_session({"Origin": self.web, "Referer": self.web + "/"})
        self.token: Optional[str] = None

    # ---------------------------------------------------------------- auth
    def _headers(self) -> Dict[str, str]:
        return {"DomainKey": self.domain, "OrganizationId": str(self.org_id or ""),
                "Authorization": f"Bearer {self.token or ''}"}

    def bootstrap(self) -> None:
        if not self.org_id:
            info = get_json(self.session, f"{API_ROOT}/organizacoes/filiais/dominio/{self.domain}",
                            headers={"Authorization": "Bearer"}, log_prefix=self.log) or {}
            self.org_id = ((info.get("data") or {}).get("organizacao") or {}).get("id")
            if not self.org_id:
                raise RuntimeError(f"{self.key}: could not resolve the VipCommerce organisation for {self.domain}")
        env_token = os.getenv(f"{self.key.upper()}_API_TOKEN")
        if env_token:
            self.token = env_token.strip()
            return
        key = os.getenv("VIPCOMMERCE_LOGIN_KEY") or DEFAULT_LOGIN_KEY
        body = post_json(self.session, f"{API_ROOT}/org/{self.org_id}/auth/loja/login",
                         json_body={"domain": self.domain, "username": "loja", "key": key},
                         headers={"DomainKey": self.domain, "OrganizationId": str(self.org_id), "Authorization": "Bearer"},
                         log_prefix=self.log)
        token = (body or {}).get("data") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token.startswith("ey"):
            raise RuntimeError(f"{self.key}: VipCommerce login failed (set {self.key.upper()}_API_TOKEN or VIPCOMMERCE_LOGIN_KEY)")
        self.token = token
        print(f"{self.log}VipCommerce org={self.org_id} token ok")

    # ---------------------------------------------------------------- store
    def store(self, db, zip_code: str) -> str:
        base = f"{API_ROOT}/org/{self.org_id}"
        data = get_json(self.session, f"{base}/loja/centros_distribuicoes/{DELIVERY_CD}", headers=self._headers(), log_prefix=self.log) or {}
        store = data.get("data") if isinstance(data.get("data"), dict) else {}
        served = get_json(self.session, f"{base}/filial/{FILIAL}/loja/tipo_entregas/realiza_entrega",
                          params={"cep": normalize_zip(zip_code)}, headers=self._headers(), max_attempts=2)
        if isinstance(served, dict) and served.get("data") is False:
            print(f"{self.log}WARNING: CEP {zip_code} outside the delivery area (catalogue is the same)")
        addr = store.get("endereco") or {}
        geo = store.get("coordenada_geografica") or {}
        store_id = f"{self.key}:{FILIAL}:{DELIVERY_CD}"
        db.save_store_info(
            store_id, query_zip=format_zip(zip_code), name=store.get("nome_site") or store.get("nome") or f"{self.key} (delivery)",
            address=", ".join(str(p) for p in [addr.get("logradouro"), addr.get("numero"), addr.get("bairro")] if p) or None,
            city=addr.get("cidade"), state=addr.get("estado"), store_zip=format_zip(addr.get("cep")) if addr.get("cep") else None,
            latitude=geo.get("latitude"), longitude=geo.get("longitude"),
            payload={k: v for k, v in store.items() if not isinstance(v, (list, dict))} if store else None,
        )
        print(f"{self.log}store {store.get('nome_site') or store.get('nome')} ({addr.get('cidade')})")
        return store_id

    # ---------------------------------------------------------------- catalogue
    def departments(self) -> List[Dict[str, Any]]:
        base = f"{API_ROOT}/org/{self.org_id}/filial/{FILIAL}/centro_distribuicao/{DELIVERY_CD}/loja/classificacoes_mercadologicas/departamentos"
        tree = get_json(self.session, f"{base}/arvore", headers=self._headers(), log_prefix=self.log) or {}
        return [d for d in (tree.get("data") or []) if isinstance(d, dict) and d.get("classificacao_mercadologica_id")]

    def offer(self, p: Dict[str, Any], store_id: str, dept: str) -> Optional[Dict[str, Any]]:
        pid = p.get("produto_id") or p.get("id")
        oferta = p.get("oferta") or {}
        promo = oferta.get("preco_oferta")
        regular = p.get("preco")
        if p.get("exibe_preco_original") and to_float(p.get("preco_original")):
            promo = promo or p.get("preco")
            regular = p.get("preco_original")
        image = p.get("imagem")
        image_url = image if str(image or "").startswith("http") else (f"{IMAGE_BASE}/{image}" if image else None)
        link = p.get("link")
        tag = oferta.get("tag") or oferta.get("nome")
        marca = p.get("marca")
        return make_offer(
            product_id=pid, store_id=store_id, product_name=p.get("descricao"),
            regular_price=regular, promo_price=promo,
            barcode=first_gtin(p.get("codigo_barras"), allow_gtin8=True), barcode_source="inline",
            brand=marca.get("descricao") if isinstance(marca, dict) else marca,
            category_path=dept, promo_min_quantity=oferta.get("quantidade_minima"),
            offer_name=oferta.get("nome"), offer_tag=tag,
            app_membership_required=any(w in str(tag).lower() for w in ("clube", "app", "cadastr")) if tag else None,
            unit=p.get("unidade_sigla"), is_available=p.get("disponivel"), stock=p.get("quantidade_maxima"),
            product_url=f"{self.web}/produto/{pid}/{link}" if pid and link else None, image_url=image_url,
        )

    def scrape(self, db, zip_code: str, limit: Optional[int] = None) -> Dict[str, int]:
        self.bootstrap()
        store_id = self.store(db, zip_code)
        depts = self.departments()
        print(f"{self.log}{len(depts)} departments")
        base = f"{API_ROOT}/org/{self.org_id}/filial/{FILIAL}/centro_distribuicao/{DELIVERY_CD}/loja/classificacoes_mercadologicas/departamentos"
        seen: set = set()
        total = {"upserted": 0, "skipped": 0, "with_barcode": 0}
        for dept in depts:
            did = dept["classificacao_mercadologica_id"]
            name = str(dept.get("descricao") or did)
            page, pages, added = 1, 1, 0
            while page <= pages and page <= 500:
                body = get_json(self.session, f"{base}/{did}/produtos", params={"page": page}, headers=self._headers(), log_prefix=self.log)
                if not body:
                    break
                products = body.get("data") or []
                pages = int((body.get("paginator") or {}).get("total_pages") or 1)
                batch = []
                for p in products:
                    if not isinstance(p, dict):
                        continue
                    offer = self.offer(p, store_id, name)
                    if offer and offer["product_id"] not in seen:
                        seen.add(offer["product_id"])
                        batch.append(offer)
                    if limit and len(seen) >= limit:
                        break
                if batch:
                    r = db.save(batch)
                    for k in total:
                        total[k] += r[k]
                    added += len(batch)
                if not products or (limit and len(seen) >= limit):
                    break
                page += 1
                time.sleep(0.08)
            print(f"{self.log}{name[:40]:<40} +{added:>5} pages={pages} (total {len(seen)})")
            if limit and len(seen) >= limit:
                break
        return total
