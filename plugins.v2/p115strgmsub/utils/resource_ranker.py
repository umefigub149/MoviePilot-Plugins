"""
HDHive/分享资源包评分排序（全类型闭环）

场景：
- 电视剧补缺/整季/追更/洗版
- 电影首下/洗版

原则：
1. 先硬过滤，再场景排序
2. 覆盖优先压过画质（电视剧补缺）
3. 免费优先于付费
4. 输出可解释 reason，供日志审计
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from app.log import logger

from .resource_title_parser import ParsedResourceTitle, ResourceTitleParser


@dataclass
class ScoreContext:
    media_type: str = "tv"  # movie|tv
    season: Optional[int] = None
    missing_episodes: Optional[Set[int]] = None
    total_episode: Optional[int] = None
    is_best_version: bool = False
    history_score_movie: Optional[int] = None
    history_score_by_ep: Optional[Dict[int, int]] = None
    max_unlock_points: int = 50
    max_points_per_sub: int = 20
    free_unlock_top_n: int = 3
    scene: str = ""  # auto if empty


@dataclass
class RankedResource:
    resource: Dict[str, Any]
    parsed: ParsedResourceTitle
    hard_reject: bool = False
    reject_reason: str = ""
    coverage_count: int = 0
    coverage_ratio: float = 0.0
    quality_score: int = 0
    free: bool = True
    unlock_points: int = 0
    total_score: float = 0.0
    rank_key: Tuple = field(default_factory=tuple)
    reason: str = ""

    def attach_to_resource(self) -> Dict[str, Any]:
        r = dict(self.resource)
        r["episode_span"] = self.parsed.ep_span
        r["package_kind"] = self.parsed.package_kind
        r["parse_confidence"] = self.parsed.confidence
        r["coverage_count"] = self.coverage_count
        r["coverage_ratio"] = self.coverage_ratio
        r["quality_score"] = self.quality_score
        r["rank_score"] = self.total_score
        r["rank_reason"] = self.reason
        r["season_hint"] = self.parsed.season_hint
        r["complete_hint"] = self.parsed.complete_hint
        r["invalid_hint"] = self.parsed.invalid_hint
        return r


class ResourceRanker:
    """全类型资源包评分器"""

    @classmethod
    def rank(
        cls,
        resources: List[Dict[str, Any]],
        context: Optional[ScoreContext] = None,
    ) -> List[Dict[str, Any]]:
        context = context or ScoreContext()
        if not resources:
            return []

        scene = context.scene or cls._detect_scene(context)
        ranked: List[RankedResource] = []
        for raw in resources:
            item = cls._score_one(raw, context, scene)
            if item.hard_reject:
                logger.info(
                    f"【HDHive评分】淘汰: {item.reject_reason} | title={str(raw.get('title') or '')[:80]}"
                )
                continue
            ranked.append(item)

        ranked.sort(key=lambda x: x.rank_key)
        out = [x.attach_to_resource() for x in ranked]

        # 日志 TopN
        top = ranked[:8]
        if top:
            bits = []
            for i, x in enumerate(top, 1):
                cost = "免费" if x.free else f"付费{x.unlock_points}"
                bits.append(
                    f"#{i} {cost} cover={x.coverage_count}"
                    f" span={x.parsed.ep_span} conf={x.parsed.confidence:.2f} "
                    f"q={x.quality_score} kind={x.parsed.package_kind} "
                    f"title={str(x.resource.get('title') or '')[:36]}"
                )
            missing_n = len(context.missing_episodes or [])
            logger.info(
                f"【HDHive评分】类型={context.media_type} 场景={scene} "
                f"季={context.season} 缺失={missing_n} 候选={len(out)} | "
                + " || ".join(bits)
            )
        return out

    @classmethod
    def _detect_scene(cls, ctx: ScoreContext) -> str:
        if (ctx.media_type or "").lower() == "movie":
            return "movie_upgrade" if ctx.is_best_version else "movie_first"
        missing = ctx.missing_episodes or set()
        if ctx.is_best_version:
            return "tv_upgrade"
        if not missing:
            return "tv_full_season"
        # 缺集少且偏后半 = 追更倾向
        if ctx.total_episode and len(missing) <= max(3, int(ctx.total_episode * 0.25)):
            if max(missing) >= max(1, int((ctx.total_episode or 1) * 0.6)):
                return "tv_follow"
        return "tv_fill_missing"

    @classmethod
    def _score_one(
        cls,
        raw: Dict[str, Any],
        ctx: ScoreContext,
        scene: str,
    ) -> RankedResource:
        title = str(raw.get("title") or "")
        parsed = ResourceTitleParser.parse(title, target_season=ctx.season)
        unlock_points = int(raw.get("unlock_points") or 0)
        free = not bool(raw.get("need_unlock", False)) and unlock_points <= 0
        # need_unlock True => paid pending
        if raw.get("need_unlock"):
            free = False
            unlock_points = int(raw.get("unlock_points") or unlock_points or 0)
        elif raw.get("is_free") is False and unlock_points > 0:
            free = False

        item = RankedResource(
            resource=raw,
            parsed=parsed,
            free=free,
            unlock_points=unlock_points,
        )

        # hard reject
        slug = str(raw.get("slug") or "")
        url = str(raw.get("url") or "")
        if not slug and not url:
            item.hard_reject = True
            item.reject_reason = "无slug且无url"
            return item
        if parsed.invalid_hint:
            item.hard_reject = True
            item.reject_reason = "疑似失效"
            return item
        if not free:
            if unlock_points > int(ctx.max_points_per_sub or 0) > 0:
                item.hard_reject = True
                item.reject_reason = f"超出单订阅预算({unlock_points}>{ctx.max_points_per_sub})"
                return item
            if unlock_points > int(ctx.max_unlock_points or 0) > 0:
                item.hard_reject = True
                item.reject_reason = f"超出全局预算({unlock_points}>{ctx.max_unlock_points})"
                return item

        # season mismatch
        if (
            (ctx.media_type or "").lower() == "tv"
            and ctx.season is not None
            and parsed.season_hint is not None
            and int(parsed.season_hint) != int(ctx.season)
        ):
            # 明确写了其他季，降权但电视剧合集可能误伤；若有 range 且季明确，拒绝
            if parsed.package_kind in {"range_pack", "single_ep", "complete_season", "progress_pack"}:
                item.hard_reject = True
                item.reject_reason = f"季不匹配(目标S{ctx.season},标题S{parsed.season_hint})"
                return item

        # media type soft conflict
        if (ctx.media_type or "").lower() == "movie" and parsed.media_guess == "tv" and parsed.package_kind in {
            "range_pack",
            "progress_pack",
            "single_ep",
        }:
            # movie sub but tv pack: reject
            item.hard_reject = True
            item.reject_reason = "电影订阅命中剧集包"
            return item

        # coverage
        missing = set(ctx.missing_episodes or set())
        if missing and parsed.ep_set:
            cover = parsed.ep_set.intersection(missing)
            item.coverage_count = len(cover)
            item.coverage_ratio = item.coverage_count / max(len(missing), 1)
        elif missing and parsed.package_kind == "complete_season":
            # unknown exact eps, assume high potential but lower confidence
            item.coverage_count = max(1, int(len(missing) * 0.8))
            item.coverage_ratio = 0.8 * parsed.confidence
        elif not missing and (ctx.media_type or "").lower() == "tv":
            # full season first download
            if parsed.ep_span > 0:
                item.coverage_count = parsed.ep_span
                item.coverage_ratio = min(1.0, parsed.ep_span / max(int(ctx.total_episode or parsed.ep_span), 1))
            elif parsed.complete_hint:
                item.coverage_count = int(ctx.total_episode or 50)
                item.coverage_ratio = 0.9
            else:
                item.coverage_count = 0
                item.coverage_ratio = 0.0
        else:
            # movie
            item.coverage_count = 0
            item.coverage_ratio = 0.0

        item.quality_score = cls._quality_score(parsed, title, bool(raw.get("is_official")))
        item.total_score = cls._total_score(item, ctx, scene)
        item.rank_key = cls._rank_key(item, ctx, scene)
        item.reason = (
            f"scene={scene},kind={parsed.package_kind},free={item.free},"
            f"cover={item.coverage_count},span={parsed.ep_span},"
            f"conf={parsed.confidence:.2f},q={item.quality_score},pts={item.unlock_points}"
        )
        return item

    @classmethod
    def _quality_score(cls, parsed: ParsedResourceTitle, title: str, is_official: bool) -> int:
        score = 0
        res = (parsed.resolution_hint or "").lower()
        if "8k" in res:
            score += 50
        elif "4k" in res or "2160" in res:
            score += 40
        elif "1080" in res:
            score += 25
        elif "720" in res:
            score += 10
        else:
            # title fallback
            t = title.lower()
            if "2160" in t or "4k" in t:
                score += 40
            elif "1080" in t:
                score += 25
            elif "720" in t:
                score += 10

        q = (parsed.quality_hint or "").lower()
        t = title.lower()
        if "web-dl" in q or "webdl" in t or "web-dl" in t:
            score += 12
        if "webrip" in q or "webrip" in t:
            score += 8
        if "remux" in q or "remux" in t:
            score += 15
        if "bluray" in q or "blu-ray" in t or "原盘" in title or "iso" in t:
            score += 10
        if "hdr" in q or "hdr" in t or "dv" in q or "dovi" in t or "杜比视界" in title:
            score += 6
        if is_official or "官组" in title or "管理员" in title:
            score += 8
        if "纯净" in title or "去广告" in title or "无水印" in title:
            score += 4
        if "hiveweb" in t or "ubweb" in t or "adweb" in t or "mweb" in t:
            score += 3
        return score

    @classmethod
    def _total_score(cls, item: RankedResource, ctx: ScoreContext, scene: str) -> float:
        free_bonus = 200 if item.free else 0
        cover = item.coverage_count * 1000
        ratio = item.coverage_ratio * 300
        conf = item.parsed.confidence
        q = item.quality_score
        cost = 0 if item.free else item.unlock_points * 15
        complete_bonus = 80 if item.parsed.complete_hint else 0
        if item.parsed.package_kind == "complete_season":
            complete_bonus += 40

        if scene.startswith("movie"):
            # movie: quality first after free
            base = free_bonus * 5 + q * 20 - cost + (30 if item.resource.get("is_official") else 0)
            if scene == "movie_upgrade" and ctx.history_score_movie is not None:
                # prefer likely upgrades
                base += max(0, q - int(ctx.history_score_movie or 0)) * 3
            return float(base)

        if scene == "tv_upgrade":
            # upgrade: quality weighted more, still need some coverage
            return float(
                free_bonus
                + cover * 0.6
                + ratio
                + q * 8
                + complete_bonus
                - cost
            ) * conf

        if scene == "tv_follow":
            # follow: latest coverage critical
            latest_bonus = 0
            missing = set(ctx.missing_episodes or set())
            if missing and item.parsed.ep_set:
                if max(missing) in item.parsed.ep_set:
                    latest_bonus = 500
            if item.parsed.package_kind == "progress_pack":
                latest_bonus += 120
            return float(free_bonus + cover + ratio + latest_bonus + q - cost) * max(conf, 0.4)

        # tv_fill_missing / tv_full_season
        return float(free_bonus + cover + ratio + complete_bonus + q - cost) * max(conf, 0.35)

    @classmethod
    def _rank_key(cls, item: RankedResource, ctx: ScoreContext, scene: str) -> Tuple:
        # lower is better for tuple sort? use inverted numeric for desc fields
        free_rank = 0 if item.free else 1
        if scene.startswith("movie"):
            return (
                free_rank,
                -item.quality_score,
                -int(bool(item.resource.get("is_official"))),
                item.unlock_points if not item.free else 0,
                -item.parsed.confidence,
                -item.total_score,
            )

        if scene == "tv_upgrade":
            return (
                free_rank,
                -item.coverage_count,
                -item.quality_score,
                -item.coverage_ratio,
                item.unlock_points if not item.free else 0,
                -item.parsed.confidence,
                -item.total_score,
            )

        # default tv fill/follow/full
        return (
            free_rank,
            -item.coverage_count,
            -item.coverage_ratio,
            -item.parsed.ep_span,
            -item.parsed.confidence,
            item.unlock_points if not item.free else 0,
            -item.quality_score,
            -int(bool(item.resource.get("is_official"))),
            -item.total_score,
        )


def sort_share_resources(
    resources: List[Dict[str, Any]],
    context: Optional[ScoreContext] = None,
) -> List[Dict[str, Any]]:
    """兼容旧接口：无上下文时退化为通用免费+span排序。"""
    if context is None:
        # lightweight default: free first, span desc
        tmp_ctx = ScoreContext(media_type="tv", scene="tv_full_season")
        return ResourceRanker.rank(resources, tmp_ctx)
    return ResourceRanker.rank(resources, context)
