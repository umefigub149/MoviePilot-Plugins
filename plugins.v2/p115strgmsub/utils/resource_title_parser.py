"""
HDHive/分享资源标题解析器（全类型通用）

只做标题启发式解析，不访问网络、不解锁。
输出供 ResourceRanker 使用的结构化字段。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class ParsedResourceTitle:
    title: str = ""
    package_kind: str = "unknown"  # range_pack|progress_pack|complete_season|single_ep|movie_pack|invalid|noise|unknown
    media_guess: str = "unknown"  # movie|tv|unknown
    season_hint: Optional[int] = None
    ep_start: Optional[int] = None
    ep_end: Optional[int] = None
    ep_set: Set[int] = field(default_factory=set)
    ep_span: int = 0
    progress_to: Optional[int] = None
    complete_hint: bool = False
    invalid_hint: bool = False
    single_episode: bool = False
    confidence: float = 0.2
    resolution_hint: str = ""
    quality_hint: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "package_kind": self.package_kind,
            "media_guess": self.media_guess,
            "season_hint": self.season_hint,
            "ep_start": self.ep_start,
            "ep_end": self.ep_end,
            "ep_set": sorted(self.ep_set),
            "ep_span": self.ep_span,
            "progress_to": self.progress_to,
            "complete_hint": self.complete_hint,
            "invalid_hint": self.invalid_hint,
            "single_episode": self.single_episode,
            "confidence": self.confidence,
            "resolution_hint": self.resolution_hint,
            "quality_hint": self.quality_hint,
            "notes": list(self.notes),
        }


class ResourceTitleParser:
    """资源标题解析器"""

    _RANGE_PATTERNS = [
        # S01E01-E16 / S01E01—E16 / S01 E01-E16 / E01-E12
        re.compile(
            r"(?:[Ss](?P<season>\d{1,2})\s*)?[Ee](?P<start>\d{1,3})\s*[-—~～到至]\s*[Ee]?(?P<end>\d{1,3})",
            re.IGNORECASE,
        ),
        # 第1-16集
        re.compile(r"第\s*(?P<start>\d{1,3})\s*[-—~～到至]\s*(?P<end>\d{1,3})\s*集"),
        # 1-16 / 01-16（排除年份）
        re.compile(r"(?<!\d)(?P<start>\d{1,2})\s*[-—~～到至]\s*(?P<end>\d{1,2})(?!\d)"),
    ]
    _PROGRESS_PATTERNS = [
        re.compile(r"更新至\s*第?\s*(?P<n>\d{1,3})\s*集?", re.IGNORECASE),
        re.compile(r"已更新(?:了)?(?:最新集)?第?\s*(?P<n>\d{1,3})\s*集?", re.IGNORECASE),
        re.compile(r"更新至第(?P<n>\d{1,3})集", re.IGNORECASE),
    ]
    _SINGLE_EP_PATTERNS = [
        re.compile(r"(?:[Ss](?P<season>\d{1,2}))?[Ee](?P<ep>\d{1,3})(?!\s*[-—~～到至])", re.IGNORECASE),
        re.compile(r"第\s*(?P<ep>\d{1,3})\s*集"),
    ]
    _SEASON_PATTERNS = [
        re.compile(r"[Ss](?P<season>\d{1,2})(?![Ee0-9])"),
        re.compile(r"[Ss]eason\s*(?P<season>\d{1,2})", re.IGNORECASE),
        re.compile(r"第\s*(?P<season>\d{1,2})\s*季"),
        re.compile(r"(?P<season>\d{1,2})\s*季"),
    ]
    _MOVIE_HINTS = re.compile(
        r"BluRay|BDRip|REMUX|UHD|ISO|原盘|蓝光|WEB-?DL|WEBRip|HDTV|\.20\d{2}\.|2160p|1080p|720p",
        re.IGNORECASE,
    )
    _TV_HINTS = re.compile(
        r"[Ss]\d{1,2}\s*[Ee]\d{1,3}|第\s*\d+\s*集|更新至|已完结|完结|Season\s*\d+",
        re.IGNORECASE,
    )
    _INVALID_HINTS = re.compile(r"疑似失效|失效|已和谐|已失效|链接失效")
    _COMPLETE_HINTS = re.compile(r"已完结|完结|Complete|COMPLETE")
    _RES_HINT = re.compile(r"\b(8K|4K|2160p|1080p|1080i|720p|480p)\b", re.IGNORECASE)
    _QUALITY_HINT = re.compile(
        r"WEB-?DL|WEBRip|BDRip|BluRay|REMUX|HDTV|DV|DoVi|HDR10\+?|HDR|HDRVivid",
        re.IGNORECASE,
    )

    @classmethod
    def parse(cls, title: str, target_season: Optional[int] = None) -> ParsedResourceTitle:
        text = str(title or "").strip()
        result = ParsedResourceTitle(title=text)
        if not text:
            result.package_kind = "noise"
            result.confidence = 0.1
            result.notes.append("empty_title")
            return result

        if cls._INVALID_HINTS.search(text):
            result.invalid_hint = True
            result.package_kind = "invalid"
            result.confidence = 0.95
            result.notes.append("invalid_hint")
            return result

        if cls._COMPLETE_HINTS.search(text):
            result.complete_hint = True
            result.notes.append("complete_hint")

        m_res = cls._RES_HINT.search(text)
        if m_res:
            result.resolution_hint = m_res.group(1).upper().replace("P", "p")
        m_q = cls._QUALITY_HINT.search(text)
        if m_q:
            result.quality_hint = m_q.group(0)

        # season
        season = cls._extract_season(text)
        if season is not None:
            result.season_hint = season

        # range first
        ep_start, ep_end, conf, note = cls._extract_range(text)
        if ep_start is not None and ep_end is not None:
            if ep_end < ep_start:
                ep_start, ep_end = ep_end, ep_start
            result.ep_start = ep_start
            result.ep_end = ep_end
            result.ep_set = set(range(ep_start, ep_end + 1))
            result.ep_span = ep_end - ep_start + 1
            result.package_kind = "range_pack"
            result.media_guess = "tv"
            result.confidence = conf
            result.notes.append(note)
            return result

        # progress
        progress = cls._extract_progress(text)
        if progress is not None:
            result.progress_to = progress
            result.ep_start = 1
            result.ep_end = progress
            result.ep_set = set(range(1, progress + 1))
            result.ep_span = progress
            result.package_kind = "progress_pack"
            result.media_guess = "tv"
            result.confidence = 0.75
            result.notes.append("progress_to")
            return result

        # complete season without explicit eps
        if result.complete_hint and (result.season_hint is not None or re.search(r"[Ss]\d{1,2}|季", text)):
            result.package_kind = "complete_season"
            result.media_guess = "tv"
            result.confidence = 0.55
            # unknown exact span; keep 0, ranker will treat specially
            result.notes.append("complete_season_no_eps")
            return result

        # single episode
        se = cls._extract_single_ep(text)
        if se is not None:
            ep, seas, conf = se
            result.single_episode = True
            result.ep_start = ep
            result.ep_end = ep
            result.ep_set = {ep}
            result.ep_span = 1
            result.package_kind = "single_ep"
            result.media_guess = "tv"
            result.confidence = conf
            if seas is not None:
                result.season_hint = seas
            result.notes.append("single_ep")
            return result

        # movie-ish
        if cls._MOVIE_HINTS.search(text) and not cls._TV_HINTS.search(text):
            result.package_kind = "movie_pack"
            result.media_guess = "movie"
            result.confidence = 0.9
            result.notes.append("movie_features")
            return result

        # noise / unknown
        if len(text) < 8 or text.count("·") >= 1 and not re.search(r"\d", text):
            result.package_kind = "noise"
            result.confidence = 0.15
            result.notes.append("noise_like")
            return result

        result.package_kind = "unknown"
        result.confidence = 0.2
        result.notes.append("unparsed")
        # optional media guess
        if cls._TV_HINTS.search(text):
            result.media_guess = "tv"
        elif cls._MOVIE_HINTS.search(text):
            result.media_guess = "movie"
        return result

    @classmethod
    def _extract_season(cls, text: str) -> Optional[int]:
        for pat in cls._SEASON_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            try:
                s = int(m.group("season"))
            except Exception:
                continue
            if 0 < s < 50:
                return s
        # S01E01 style
        m = re.search(r"[Ss](\d{1,2})[Ee]\d{1,3}", text)
        if m:
            try:
                s = int(m.group(1))
                if 0 < s < 50:
                    return s
            except Exception:
                pass
        return None

    @classmethod
    def _extract_range(cls, text: str):
        for pat in cls._RANGE_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            try:
                a = int(m.group("start"))
                b = int(m.group("end"))
            except Exception:
                continue
            if a <= 0 or b <= 0:
                continue
            if a >= 1900 or b >= 1900:
                continue
            if abs(b - a) > 200:
                continue
            conf = 0.95 if "E" in m.group(0).upper() or "e" in m.group(0) else 0.90
            if "第" in m.group(0):
                conf = 0.92
            return a, b, conf, f"range:{m.group(0)}"
        return None, None, 0.0, ""

    @classmethod
    def _extract_progress(cls, text: str) -> Optional[int]:
        for pat in cls._PROGRESS_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            try:
                n = int(m.group("n"))
            except Exception:
                continue
            if 0 < n < 1000:
                return n
        return None

    @classmethod
    def _extract_single_ep(cls, text: str):
        for pat in cls._SINGLE_EP_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            try:
                ep = int(m.group("ep"))
            except Exception:
                continue
            if not (0 < ep < 1000):
                continue
            seas = None
            if "season" in m.groupdict() and m.groupdict().get("season"):
                try:
                    seas = int(m.group("season"))
                except Exception:
                    seas = None
            return ep, seas, 0.9
        return None


def estimate_resource_episode_span(title: str) -> int:
    """兼容旧接口：仅返回 span。"""
    return ResourceTitleParser.parse(title).ep_span
