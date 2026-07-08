import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median, pstdev
from typing import Any, Dict, List, Optional, Sequence, Tuple


class HistoricalPricingAnalyzer:
    WEEKDAY_LABELS = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    EPSILON = 0.001

    def __init__(self, conn, market_name: str):
        self.conn = conn
        self.market_name = str(market_name or "").strip()

    def refresh(self, store_ids: Sequence[Optional[str]]) -> Dict[str, int]:
        normalized_store_ids = [
            str(store_id).strip()
            for store_id in store_ids
            if str(store_id or "").strip()
        ]
        if not normalized_store_ids:
            return {"stores": 0, "products": 0}

        current_offers = self._fetch_current_offers(normalized_store_ids)
        history_rows = self._fetch_history_rows(normalized_store_ids)

        offers_by_store: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        history_by_store: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        history_by_offer: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        for offer in current_offers:
            offers_by_store[str(offer["store_id"])].append(offer)

        for row in history_rows:
            store_id = str(row["store_id"])
            offer_id = str(row["offer_id"])
            history_by_store[store_id].append(row)
            history_by_offer[offer_id].append(row)

        analyzed_at = datetime.now().isoformat()
        store_insights: List[Tuple[Any, ...]] = []
        product_patterns: List[Tuple[Any, ...]] = []

        for store_id in normalized_store_ids:
            store_history = history_by_store.get(store_id, [])
            store_offers = offers_by_store.get(store_id, [])
            store_insight = self._build_store_insight(
                store_id=store_id,
                store_offers=store_offers,
                store_history=store_history,
                analyzed_at=analyzed_at,
            )
            if store_insight is not None:
                store_insights.append(store_insight)

            for offer in store_offers:
                product_patterns.append(
                    self._build_product_pattern(
                        offer=offer,
                        history_rows=history_by_offer.get(str(offer["id"]), []),
                        analyzed_at=analyzed_at,
                    )
                )

        cursor = self.conn.cursor()
        cursor.execute(
            "DELETE FROM product_price_patterns WHERE market_name = %s AND store_id = ANY(%s)",
            (self.market_name, normalized_store_ids),
        )
        cursor.execute(
            "DELETE FROM store_pricing_insights WHERE market_name = %s AND store_id = ANY(%s)",
            (self.market_name, normalized_store_ids),
        )

        if store_insights:
            cursor.executemany(
                """
                INSERT INTO store_pricing_insights (
                    market_name,
                    store_id,
                    best_buy_weekday,
                    best_buy_weekday_label,
                    best_weekday_avg_discount_pct,
                    best_weekday_promo_rate,
                    best_weekday_avg_effective_price,
                    overall_promo_rate,
                    overall_avg_discount_pct,
                    total_price_events,
                    total_products,
                    analyzed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                store_insights,
            )

        if product_patterns:
            cursor.executemany(
                """
                INSERT INTO product_price_patterns (
                    market_name,
                    store_id,
                    offer_id,
                    product_name,
                    current_regular_price,
                    current_promo_price,
                    current_effective_price,
                    observed_min_price,
                    observed_max_price,
                    low_price_mode,
                    high_price_mode,
                    price_points_json,
                    pattern_type,
                    samples_count,
                    best_buy_weekday,
                    best_buy_weekday_label,
                    best_weekday_price,
                    avg_toggle_interval_days,
                    toggle_interval_std_days,
                    predicted_next_toggle_at,
                    predicted_next_price,
                    predicted_direction,
                    prediction_confidence,
                    prediction_source,
                    last_observed_change_at,
                    promo_end_at,
                    analyzed_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                """,
                product_patterns,
            )

        return {
            "stores": len(store_insights),
            "products": len(product_patterns),
        }

    def _fetch_current_offers(self, store_ids: Sequence[str]) -> List[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT
                id,
                store_id,
                product_name,
                regular_price,
                promo_price,
                promo_end_at,
                last_updated
            FROM offers
            WHERE market_name = %s
              AND store_id = ANY(%s)
            """,
            (self.market_name, list(store_ids)),
        )
        return self._rows_to_dicts(cursor)

    def _fetch_history_rows(self, store_ids: Sequence[str]) -> List[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT
                offer_id,
                store_id,
                product_name,
                regular_price,
                promo_price,
                recorded_at
            FROM price_history
            WHERE market_name = %s
              AND store_id = ANY(%s)
            ORDER BY store_id, offer_id, recorded_at
            """,
            (self.market_name, list(store_ids)),
        )
        return self._rows_to_dicts(cursor)

    @staticmethod
    def _rows_to_dicts(cursor) -> List[Dict[str, Any]]:
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def _build_store_insight(
        self,
        store_id: str,
        store_offers: Sequence[Dict[str, Any]],
        store_history: Sequence[Dict[str, Any]],
        analyzed_at: str,
    ) -> Optional[Tuple[Any, ...]]:
        weekday_stats: Dict[int, Dict[str, float]] = defaultdict(
            lambda: {
                "events": 0.0,
                "promo_events": 0.0,
                "discount_sum": 0.0,
                "effective_sum": 0.0,
            }
        )

        for row in store_history:
            recorded_at = self._as_datetime(row.get("recorded_at"))
            effective_price = self._effective_price(
                row.get("regular_price"),
                row.get("promo_price"),
            )
            if recorded_at is None or effective_price is None:
                continue

            weekday = recorded_at.weekday()
            stats = weekday_stats[weekday]
            stats["events"] += 1.0
            stats["effective_sum"] += effective_price

            discount_pct = self._discount_pct(row.get("regular_price"), row.get("promo_price"))
            if discount_pct > 0:
                stats["promo_events"] += 1.0
                stats["discount_sum"] += discount_pct

        total_events = int(sum(stats["events"] for stats in weekday_stats.values()))
        total_products = len(store_offers)
        if total_events == 0 and total_products == 0:
            return None

        best_weekday: Optional[int] = None
        best_stats: Optional[Dict[str, float]] = None
        best_score: Optional[Tuple[float, float, float, float]] = None
        for weekday, stats in weekday_stats.items():
            events = max(stats["events"], 1.0)
            avg_discount = stats["discount_sum"] / events
            promo_rate = stats["promo_events"] / events
            avg_effective = stats["effective_sum"] / events
            score = (avg_discount, promo_rate, -avg_effective, stats["events"])
            if best_score is None or score > best_score:
                best_weekday = weekday
                best_stats = stats
                best_score = score

        overall_promo_events = sum(stats["promo_events"] for stats in weekday_stats.values())
        overall_discount = sum(stats["discount_sum"] for stats in weekday_stats.values())
        best_events = max(best_stats["events"], 1.0) if best_stats else 1.0

        return (
            self.market_name,
            store_id,
            best_weekday,
            self.WEEKDAY_LABELS[best_weekday] if best_weekday is not None else None,
            round((best_stats["discount_sum"] / best_events), 4) if best_stats else None,
            round((best_stats["promo_events"] / best_events), 4) if best_stats else None,
            round((best_stats["effective_sum"] / best_events), 4) if best_stats else None,
            round((overall_promo_events / total_events), 4) if total_events else 0.0,
            round((overall_discount / total_events), 4) if total_events else 0.0,
            total_events,
            total_products,
            analyzed_at,
        )

    def _build_product_pattern(
        self,
        offer: Dict[str, Any],
        history_rows: Sequence[Dict[str, Any]],
        analyzed_at: str,
    ) -> Tuple[Any, ...]:
        current_regular_price = self._coerce_float(offer.get("regular_price"))
        current_promo_price = self._coerce_float(offer.get("promo_price"))
        current_effective_price = self._effective_price(current_regular_price, current_promo_price)

        observations: List[Tuple[datetime, float]] = []
        weekday_prices: Dict[int, List[float]] = defaultdict(list)
        for row in history_rows:
            recorded_at = self._as_datetime(row.get("recorded_at"))
            effective_price = self._effective_price(
                row.get("regular_price"),
                row.get("promo_price"),
            )
            if recorded_at is None or effective_price is None:
                continue
            rounded_price = round(effective_price, 2)
            observations.append((recorded_at, rounded_price))
            weekday_prices[recorded_at.weekday()].append(rounded_price)

        observations.sort(key=lambda item: item[0])
        if not observations and current_effective_price is not None:
            last_updated = self._as_datetime(offer.get("last_updated")) or datetime.now()
            rounded_current = round(current_effective_price, 2)
            observations.append((last_updated, rounded_current))
            weekday_prices[last_updated.weekday()].append(rounded_current)

        sampled_prices = [price for _, price in observations]
        price_counter = Counter(sampled_prices)
        ranked_prices = sorted(price_counter.items(), key=lambda item: (-item[1], item[0]))
        samples_count = len(sampled_prices)
        observed_min_price = min(sampled_prices) if sampled_prices else current_effective_price
        observed_max_price = max(sampled_prices) if sampled_prices else current_effective_price

        low_price_mode: Optional[float] = None
        high_price_mode: Optional[float] = None
        if ranked_prices:
            dominant_prices = [price for price, _ in ranked_prices[:2]]
            low_price_mode = min(dominant_prices)
            high_price_mode = max(dominant_prices)

        pattern_type = self._classify_pattern(price_counter)
        price_points_json = json.dumps(
            [
                {
                    "price": price,
                    "count": count,
                    "share": round(count / samples_count, 4) if samples_count else 0.0,
                }
                for price, count in ranked_prices
            ],
            ensure_ascii=False,
        )

        best_buy_weekday: Optional[int] = None
        best_weekday_price: Optional[float] = None
        for weekday, prices in weekday_prices.items():
            avg_price = sum(prices) / len(prices)
            if best_weekday_price is None or avg_price < best_weekday_price - self.EPSILON:
                best_buy_weekday = weekday
                best_weekday_price = avg_price
            elif (
                best_weekday_price is not None
                and abs(avg_price - best_weekday_price) <= self.EPSILON
                and best_buy_weekday is not None
                and len(prices) > len(weekday_prices.get(best_buy_weekday, []))
            ):
                best_buy_weekday = weekday
                best_weekday_price = avg_price

        toggle_events = self._dedupe_price_transitions(observations)
        interval_days = [
            (toggle_events[index][0] - toggle_events[index - 1][0]).total_seconds() / 86400.0
            for index in range(1, len(toggle_events))
            if (toggle_events[index][0] - toggle_events[index - 1][0]).total_seconds() > 0
        ]
        avg_toggle_interval_days: Optional[float] = None
        toggle_interval_std_days: Optional[float] = None
        predicted_next_toggle_at: Optional[str] = None
        prediction_source: Optional[str] = None

        if offer.get("promo_end_at"):
            predicted_next_toggle_at = self._iso_datetime(offer.get("promo_end_at"))
            prediction_source = "promo_end_at"
        elif interval_days:
            median_interval = float(median(interval_days))
            avg_toggle_interval_days = round(median_interval, 4)
            if len(interval_days) > 1:
                toggle_interval_std_days = round(float(pstdev(interval_days)), 4)
            last_change_at = toggle_events[-1][0] if toggle_events else None
            if last_change_at is not None:
                predicted_next_toggle_at = (last_change_at + timedelta(days=median_interval)).isoformat()
                prediction_source = "historical_toggle_pattern"

        predicted_next_price = self._predict_next_price(
            current_effective_price=current_effective_price,
            current_regular_price=current_regular_price,
            current_promo_price=current_promo_price,
            low_price_mode=low_price_mode,
            high_price_mode=high_price_mode,
            pattern_type=pattern_type,
            prediction_source=prediction_source,
        )
        predicted_direction = self._predict_direction(current_effective_price, predicted_next_price)
        prediction_confidence = self._prediction_confidence(
            price_counter=price_counter,
            interval_days=interval_days,
            pattern_type=pattern_type,
            prediction_source=prediction_source,
        )

        return (
            self.market_name,
            str(offer.get("store_id") or "").strip(),
            str(offer.get("id") or "").strip(),
            offer.get("product_name"),
            current_regular_price,
            current_promo_price,
            current_effective_price,
            observed_min_price,
            observed_max_price,
            low_price_mode,
            high_price_mode,
            price_points_json,
            pattern_type,
            samples_count,
            best_buy_weekday,
            self.WEEKDAY_LABELS[best_buy_weekday] if best_buy_weekday is not None else None,
            round(best_weekday_price, 4) if best_weekday_price is not None else None,
            avg_toggle_interval_days,
            toggle_interval_std_days,
            predicted_next_toggle_at,
            predicted_next_price,
            predicted_direction,
            prediction_confidence,
            prediction_source,
            toggle_events[-1][0].isoformat() if toggle_events else None,
            self._iso_datetime(offer.get("promo_end_at")),
            analyzed_at,
        )

    @classmethod
    def _classify_pattern(cls, price_counter: Counter) -> str:
        total = sum(price_counter.values())
        distinct = len(price_counter)
        if total <= 1 or distinct <= 1:
            return "stable"

        ranked = price_counter.most_common()
        dominant_share = ranked[0][1] / total if ranked else 0.0
        top_two_share = sum(count for _, count in ranked[:2]) / total if ranked else 0.0
        if distinct == 2 and top_two_share >= 0.75:
            return "bimodal_toggle"
        if distinct <= 3 and top_two_share >= 0.8:
            return "anchored_multi_price"
        if dominant_share >= 0.7:
            return "mostly_stable"
        return "volatile"

    @classmethod
    def _dedupe_price_transitions(
        cls,
        observations: Sequence[Tuple[datetime, float]],
    ) -> List[Tuple[datetime, float]]:
        transitions: List[Tuple[datetime, float]] = []
        previous_price: Optional[float] = None
        for recorded_at, price in observations:
            if previous_price is None or abs(price - previous_price) > cls.EPSILON:
                transitions.append((recorded_at, price))
                previous_price = price
        return transitions

    @classmethod
    def _predict_next_price(
        cls,
        current_effective_price: Optional[float],
        current_regular_price: Optional[float],
        current_promo_price: Optional[float],
        low_price_mode: Optional[float],
        high_price_mode: Optional[float],
        pattern_type: str,
        prediction_source: Optional[str],
    ) -> Optional[float]:
        if prediction_source == "promo_end_at" and current_promo_price is not None:
            return current_regular_price
        if current_effective_price is None:
            return None
        if low_price_mode is None or high_price_mode is None:
            return None
        if pattern_type not in {"bimodal_toggle", "anchored_multi_price"}:
            return None
        if abs(current_effective_price - low_price_mode) <= cls.EPSILON:
            return high_price_mode
        if abs(current_effective_price - high_price_mode) <= cls.EPSILON:
            return low_price_mode
        if current_effective_price > low_price_mode:
            return low_price_mode
        return high_price_mode

    @classmethod
    def _predict_direction(
        cls,
        current_effective_price: Optional[float],
        predicted_next_price: Optional[float],
    ) -> Optional[str]:
        if current_effective_price is None or predicted_next_price is None:
            return None
        if predicted_next_price < current_effective_price - cls.EPSILON:
            return "down"
        if predicted_next_price > current_effective_price + cls.EPSILON:
            return "up"
        return "flat"

    @classmethod
    def _prediction_confidence(
        cls,
        price_counter: Counter,
        interval_days: Sequence[float],
        pattern_type: str,
        prediction_source: Optional[str],
    ) -> Optional[float]:
        if prediction_source == "promo_end_at":
            return 1.0
        if not price_counter:
            return None
        total = sum(price_counter.values())
        ranked = price_counter.most_common()
        dominant_share = ranked[0][1] / total if total and ranked else 0.0
        interval_signal = min(len(interval_days) / 4.0, 1.0)
        if len(interval_days) > 1:
            interval_avg = sum(interval_days) / len(interval_days)
            interval_std = float(pstdev(interval_days))
            regularity = 1.0 - min(interval_std / interval_avg, 1.0) if interval_avg > 0 else 0.0
        else:
            regularity = 0.0
        pattern_bonus = 0.2 if pattern_type in {"bimodal_toggle", "anchored_multi_price"} else 0.0
        confidence = 0.2 + (dominant_share * 0.35) + (interval_signal * 0.25) + (regularity * 0.2) + pattern_bonus
        return round(max(0.0, min(confidence, 0.98)), 4)

    @classmethod
    def _effective_price(
        cls,
        regular_price: Optional[Any],
        promo_price: Optional[Any],
    ) -> Optional[float]:
        promo_value = cls._coerce_float(promo_price)
        if promo_value is not None:
            return promo_value
        return cls._coerce_float(regular_price)

    @classmethod
    def _discount_pct(
        cls,
        regular_price: Optional[Any],
        promo_price: Optional[Any],
    ) -> float:
        regular_value = cls._coerce_float(regular_price)
        promo_value = cls._coerce_float(promo_price)
        if regular_value is None or promo_value is None or regular_value <= 0:
            return 0.0
        if promo_value >= regular_value - cls.EPSILON:
            return 0.0
        return ((regular_value - promo_value) / regular_value) * 100.0

    @staticmethod
    def _coerce_float(value: Optional[Any]) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_datetime(value: Optional[Any]) -> Optional[datetime]:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    @classmethod
    def _iso_datetime(cls, value: Optional[Any]) -> Optional[str]:
        parsed = cls._as_datetime(value)
        return parsed.isoformat() if parsed is not None else None