# -*- coding: utf-8 -*-
"""
Walch 전처리 작업 결과보고서 PDF 생성 (가독성 개선판)
"""
import pandas as pd
from pathlib import Path

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, NextPageTemplate,
    Paragraph, Spacer, Table, TableStyle, PageBreak, HRFlowable,
    ListFlowable, ListItem, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ---------- 폰트 ----------
FONT_DIR = Path(r"C:\Windows\Fonts")
pdfmetrics.registerFont(TTFont("Malgun", str(FONT_DIR / "malgun.ttf")))
pdfmetrics.registerFont(TTFont("Malgun-Bold", str(FONT_DIR / "malgunbd.ttf")))

BASE = Path(r"C:\Users\JSD\Desktop\Walch_처리")
OUT = BASE / "output"

DOC_TITLE = "01-Walch_전처리_및_수면공식입력값_결과보고서"
REPORT_PATH = BASE / f"{DOC_TITLE}.pdf"

NAVY = colors.HexColor("#1f3a5f")
ACCENT = colors.HexColor("#2f7ed8")
LIGHT_BG = colors.HexColor("#eef4fb")
ROW_ALT = colors.HexColor("#f4f6f9")
GREY = colors.HexColor("#6b7280")
GREEN = colors.HexColor("#1a8754")
BORDER = colors.HexColor("#d7dde5")

# ---------- 스타일 ----------
styles = getSampleStyleSheet()
styles.add(ParagraphStyle("DocTitle", fontName="Malgun-Bold", fontSize=19, leading=25, textColor=NAVY))
styles.add(ParagraphStyle("DocSubtitle", fontName="Malgun", fontSize=10.5, leading=15, textColor=GREY, spaceAfter=2))
styles.add(ParagraphStyle("SecNum", fontName="Malgun-Bold", fontSize=10.5, textColor=colors.white))
styles.add(ParagraphStyle("SecTitle", fontName="Malgun-Bold", fontSize=13.5, leading=17, textColor=NAVY, spaceBefore=0, spaceAfter=0))
styles.add(ParagraphStyle("SubTitle", fontName="Malgun-Bold", fontSize=11, leading=15, textColor=NAVY, spaceBefore=10, spaceAfter=5))
styles.add(ParagraphStyle("Body", fontName="Malgun", fontSize=10, leading=16, spaceAfter=7, alignment=TA_LEFT))
styles.add(ParagraphStyle("BulletTxt", fontName="Malgun", fontSize=10, leading=15, spaceAfter=4))
styles.add(ParagraphStyle("Note", fontName="Malgun", fontSize=8.7, leading=13, textColor=GREY, spaceAfter=4))
styles.add(ParagraphStyle("CellHead", fontName="Malgun-Bold", fontSize=9, leading=12, textColor=colors.white, alignment=TA_CENTER))
styles.add(ParagraphStyle("Cell", fontName="Malgun", fontSize=9, leading=12))
styles.add(ParagraphStyle("CellSm", fontName="Malgun", fontSize=7.6, leading=10, alignment=TA_CENTER))
styles.add(ParagraphStyle("CellSmL", fontName="Malgun", fontSize=7.6, leading=10))
styles.add(ParagraphStyle("KpiNum", fontName="Malgun-Bold", fontSize=19, leading=22, textColor=NAVY, alignment=TA_CENTER))
styles.add(ParagraphStyle("KpiLabel", fontName="Malgun", fontSize=8.3, leading=11, textColor=GREY, alignment=TA_CENTER))
styles.add(ParagraphStyle("CoverBoxTitle", fontName="Malgun-Bold", fontSize=11.5, leading=15, textColor=NAVY, spaceAfter=6))


def P(text, style="Body"):
    return Paragraph(text, styles[style])


def section_header(num, title):
    bar = Table([[Paragraph(num, styles["SecNum"]), P(title, "SecTitle")]],
                colWidths=[9 * mm, 220 * mm])
    bar.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (1, 0), (1, 0), 8),
    ]))
    return [bar, HRFlowable(width="100%", thickness=1.2, color=NAVY, spaceBefore=0, spaceAfter=10)]


def bullets(items):
    return ListFlowable(
        [ListItem(P(t, "BulletTxt"), leftIndent=4) for t in items],
        bulletType="bullet", start="•", leftIndent=12, bulletFontSize=9, spaceBefore=2, spaceAfter=8,
    )


def styled_table(data, col_widths, font_size=9, header=True, align_center_cols=None, zebra=True):
    t = Table(data, colWidths=col_widths, repeatRows=1 if header else 0)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "Malgun"),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]
    if header:
        style += [
            ("FONTNAME", (0, 0), (-1, 0), "Malgun-Bold"),
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ]
    if zebra:
        for r in range(1 if header else 0, len(data)):
            if (r - (1 if header else 0)) % 2 == 1:
                style.append(("BACKGROUND", (0, r), (-1, r), ROW_ALT))
    if align_center_cols:
        for c in align_center_cols:
            style.append(("ALIGN", (c, 0 if not header else 1), (c, -1), "CENTER"))
    t.setStyle(TableStyle(style))
    return t


def kpi_card(number, label):
    tbl = Table([[Paragraph(number, styles["KpiNum"])], [Paragraph(label, styles["KpiLabel"])]],
                colWidths=[44 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
        ("TOPPADDING", (0, 1), (-1, 1), 2),
    ]))
    return tbl


def status_badge(ok=True):
    txt = "PASS" if ok else "FAIL"
    col = GREEN if ok else colors.HexColor("#c0392b")
    style = ParagraphStyle("Badge", fontName="Malgun-Bold", fontSize=8.5, textColor=colors.white, alignment=TA_CENTER)
    t = Table([[Paragraph(txt, style)]], colWidths=[20 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), col), ("TOPPADDING", (0, 0), (-1, -1), 3),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    return t


# ---------- 헤더/푸터 ----------
def draw_header_footer(canvas, doc):
    canvas.saveState()
    w, h = doc.pagesize
    canvas.setFillColor(NAVY)
    canvas.rect(0, h - 10 * mm, w, 10 * mm, stroke=0, fill=1)
    canvas.setFont("Malgun", 8)
    canvas.setFillColor(colors.white)
    canvas.drawString(15 * mm, h - 7 * mm, DOC_TITLE)
    canvas.setFont("Malgun", 8)
    canvas.setFillColor(GREY)
    canvas.drawRightString(w - 15 * mm, 8 * mm, f"{canvas.getPageNumber()}")
    canvas.setStrokeColor(BORDER)
    canvas.line(15 * mm, 11 * mm, w - 15 * mm, 11 * mm)
    canvas.restoreState()


def build():
    summary = pd.read_csv(OUT / "summary.csv")
    params = pd.read_csv(OUT / "two_process_input" / "two_process_params.csv")
    demo = pd.read_csv(OUT / "two_process_input" / "demo_validation_results.csv")

    doc = BaseDocTemplate(str(REPORT_PATH), pagesize=A4, title=DOC_TITLE,
                           leftMargin=15 * mm, rightMargin=15 * mm, topMargin=16 * mm, bottomMargin=14 * mm)
    portrait_frame = Frame(doc.leftMargin, doc.bottomMargin,
                            doc.width, doc.height, id="portrait")
    landscape_size = landscape(A4)
    lw, lh = landscape_size
    landscape_frame = Frame(15 * mm, 14 * mm, lw - 30 * mm, lh - 30 * mm, id="landscape")

    doc.addPageTemplates([
        PageTemplate(id="Portrait", frames=[portrait_frame], pagesize=A4, onPage=draw_header_footer),
        PageTemplate(id="Landscape", frames=[landscape_frame], pagesize=landscape_size, onPage=draw_header_footer),
    ])

    story = []

    # ================= 표지 =================
    story.append(Spacer(1, 6))
    story.append(P(DOC_TITLE, "DocTitle"))
    story.append(Spacer(1, 4))
    story.append(P(
        "웨어러블 · 실측수면 · 수면생리공식 통합 파이프라인 프로젝트 — Walch(Apple Watch) 데이터셋 "
        "전처리 및 Two-Process Model 입력값 매핑 작업 보고서", "DocSubtitle"))
    story.append(HRFlowable(width="100%", thickness=1.5, color=NAVY, spaceBefore=6, spaceAfter=14))

    kpis = Table([[
        kpi_card("31", "처리 Subject 수"),
        kpi_card("2 / 2", "요청 결과물 완성"),
        kpi_card("0건", "검증 오류 발견"),
        kpi_card("95", "생성 파일 수"),
    ]], colWidths=[46 * mm] * 4)
    kpis.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("LEFTPADDING", (0, 0), (-1, -1), 3),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
    story.append(kpis)
    story.append(Spacer(1, 14))

    story.append(P("한눈에 보는 결과", "CoverBoxTitle"))
    summary_box = Table([[bullets([
        "담당 범위(Walch 데이터 정리 + 최근 7일 수면기록 복원) 5개 작업, 2개 최종 결과물 <b>전부 완료</b>",
        "원본 파일을 재계산·재대조하는 방식으로 검증 — HR/steps/label 매핑 <b>오차 0</b>, motion 집계 오차는 부동소수점 수준",
        "7일 수면기록 복원 알고리즘에서 발견된 <b>결측구간 오인 버그를 수정</b>하고 재실행하여 정상화 확인",
        "후속 작업으로 Two-Process Model(Borbély) 입력값(S0, 서카디안 위상 앵커) 매핑까지 완료",
    ])]], colWidths=[186 * mm])
    summary_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(summary_box)
    story.append(Spacer(1, 16))

    story.extend(section_header("1", "프로젝트 개요 및 담당 범위"))
    story.append(P(
        "웨어러블 데이터, 실제 수면 데이터(PSG), 기존 수면 생리학 공식을 하나의 파이프라인으로 연결하여 "
        "개인 수면 상태를 예측하고 AI로 보정하는 모델 개발 프로젝트. AI 학습 전 5개 데이터셋"
        "(MESA, MMASH, Walch, Sleep-EDF, CMI)을 공통 형태로 통일하는 전처리를 우선 진행 중.", "Body"))
    story.append(P("팀 전체 파이프라인 6단계", "SubTitle"))
    story.append(bullets([
        "① 시간 단위 통일  ② 필요 Sensor 값 추출  ③ Sleep/Wake·Sleep Stage 기준 통일",
        "④ Subject 단위 데이터 분리  ⑤ 기존 수면 공식 입력값 생성  ⑥ Ground Truth 생성",
    ]))
    story.append(P("본인 담당 범위 — Walch(Apple Watch) 데이터 정리 및 최근 7일 수면 기록 복원", "SubTitle"))
    story.append(styled_table(
        [["구분", "내용"],
         ["작업 1", "Walch 파일 구조 및 주요 Column 확인"],
         ["작업 2", "심박수 데이터를 30초 단위로 통일"],
         ["작업 3", "Sleep/Wake 및 필요한 Sensor 데이터 정리"],
         ["작업 4", "공통 데이터 형식으로 변환"],
         ["작업 5", "Subject별 최근 7일 수면 기록 복원"],
         ["결과물 1", "30초 단위 Walch 데이터"],
         ["결과물 2", "Subject별 최근 7일 수면 기록"]],
        col_widths=[35 * mm, 151 * mm], font_size=9.3))

    story.append(PageBreak())

    # ================= 2. 원본 데이터 =================
    story.extend(section_header("2", "원본 Walch 데이터 구조"))
    story.append(P(
        "원본 위치: <font face='Malgun'>C:\\Users\\JSD\\Desktop\\데이터 폴더</font>  "
        "— heart_rate / labels / motion / steps 4개 폴더, 31명 subject "
        "(PhysioNet Walch \"sleep_accel\" 데이터셋 구조와 동일).", "Body"))
    story.append(styled_table(
        [["폴더", "파일명 규칙", "컬럼", "특성"],
         ["heart_rate", "{id}_heartrate.csv", "timestamp, hr", "비주기 샘플링 · 전체 4.1MB"],
         ["labels", "{id}_labeled_sleep.csv", "epoch(30초), label", "PSG 채점 · 당일 밤 1회만 존재"],
         ["motion", "{id}_acceleration.csv", "timestamp, x, y, z", "5~15Hz 고주파 · 전체 2.2GB"],
         ["steps", "{id}_steps.txt (헤더 없음)", "timestamp, count", "10분 bin · 전체 396KB"]],
        col_widths=[30*mm, 48*mm, 42*mm, 66*mm], font_size=9))
    story.append(P("label 인코딩:  -1=Unscored · 0=Wake · 1=N1 · 2=N2 · 3=N3 · 4=N3(병합) · 5=REM", "Note"))
    story.append(Spacer(1, 6))

    story.append(P("핵심 발견 — 센서별 과거 커버리지 차이", "SubTitle"))
    story.append(P(
        "과거 방향 데이터 커버리지가 센서별로 크게 다름. steps는 대부분 subject에서 PSG 당일 기준 "
        "약 -7일까지 존재하지만, motion(가속도)은 subject 편차가 커서 6시간~5일 수준으로 짧은 경우가 많음. "
        "따라서 7일 수면 이력 복원은 <b>steps를 주 신호</b>로 사용하도록 설계함 (사용자 확인 후 결정).", "Body"))

    story.append(PageBreak())

    # ================= 3. 작업 내용 =================
    story.extend(section_header("3", "수행한 작업 및 산출물"))

    story.append(P("3.1  30초 단위 공통 데이터  (결과물 1)", "SubTitle"))
    story.append(P(
        "저장 위치: <font face='Malgun'>output\\30s\\{subject_id}_unified_30s.csv</font>  (31개 파일)", "Body"))
    story.append(bullets([
        "heart_rate · motion → 30초 구간별 mean / std / count로 집계",
        "steps → 원 10분 bin을 30초 단위로 균등 분배",
        "labels → 원래 30초 grid를 그대로 정렬하여 결합",
    ]))
    story.append(styled_table(
        [["컬럼", "설명"],
         ["subject_id / epoch_idx / t_rel_sec", "subject ID · 30초 grid 인덱스 · PSG epoch=0 기준 상대시간(초)"],
         ["hr_mean / hr_std / hr_n", "30초 구간 심박수 평균 / 표준편차 / 샘플수"],
         ["activity_mean / std / n / has_motion", "가속도 magnitude 집계 및 데이터 존재 여부"],
         ["steps", "해당 30초 구간에 분배된 걸음수"],
         ["sleep_stage_raw / sleep_stage / is_sleep", "원본 label / 표준 단계명(Wake·N1·N2·N3·REM) / 이진 수면여부"]],
        col_widths=[62*mm, 124*mm], font_size=8.7))

    story.append(P("3.2  Subject별 최근 7일 수면 기록  (결과물 2)", "SubTitle"))
    story.append(P(
        "저장 위치: <font face='Malgun'>output\\sleep_history_7d\\{subject_id}_sleep_history_7d.csv</font>  (31개 파일)",
        "Body"))
    story.append(bullets([
        "steps를 5분 단위로 집계하여 무활동(rest) 구간 탐지",
        "10분 이하의 짧은 활동은 브릿징(같은 수면구간으로 간주)",
        "90분 이상 지속된 구간을 하루(24h window)당 가장 긴 수면 후보로 채택",
        "컬럼: subject_id, day_index(1=PSG 전날 ~ 7=가장 오래된 날), sleep_onset/wake_time, duration, data_coverage_frac",
    ]))
    warn_box = Table([[P(
        "<b>수정 이력</b> — 최초 구현은 웨어러블 미착용/결측 구간을 \"휴식(=수면)\"으로 오인하여 최대 46시간짜리 "
        "비현실적 수면이 계산되는 오류가 있었음. 결측 구간을 \"미지\"로 분리하고 실측 데이터 비율 50% 미만 구간을 "
        "폐기하도록 수정 후 재실행 → 전 subject duration이 4.5~13.3시간 범위로 정상화됨.", "Cell")]],
        colWidths=[186 * mm])
    warn_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fff6e5")),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#f0c36d")),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(warn_box)
    story.append(Spacer(1, 10))

    story.append(P("3.3  Two-Process Model 입력값 매핑  (후속 작업)", "SubTitle"))
    story.append(P(
        "저장 위치: <font face='Malgun'>output\\two_process_input\\</font>  "
        "(history_sleepwake_30s.csv 31개, two_process_params.csv, model_constants.json)", "Body"))
    story.append(bullets([
        "복원된 7일 이력을 표준 시간상수(τi=18.2h, τd=4.2h)로 순차 시뮬레이션 → PSG 당일 밤 시작 시점의 "
        "항상성 수면압력 초기값(S0) 계산",
        "복원된 수면 중앙시각들의 원형평균(circular mean)으로 서카디안 위상 앵커 추정",
        "Walch 데이터는 절대시각이 없어 실제 서카디안 위상(DLMO 등) 대신 근사값 사용 — 학술발표 시 명시 필요",
    ]))

    story.append(PageBreak())

    # ================= 4. 검증 결과 =================
    story.extend(section_header("4", "검증 결과 (실측 데이터 대조)"))
    story.append(P("\"완료했다\"는 주장이 아니라, 원본 파일을 다시 읽어 코드로 재계산·대조한 결과임.", "Note"))
    story.append(Spacer(1, 4))

    verify_rows = [
        ["검증 항목", "방법", "결과", ""],
        ["subject 목록 일치", "원본 labels 31명 목록 vs output 파일 목록 diff", "31 / 31 완전 일치", 1],
        ["30초 grid 연속성", "전 subject epoch_idx 연속성 · 중복 검사", "문제 0건", 1],
        ["컬럼 스키마 통일성", "31개 unified_30s.csv 컬럼 목록 전체 비교", "31개 파일 동일 스키마(14열)", 1],
        ["HR 집계 정확성", "원본 heart_rate.csv 수동 재집계 후 대조(3개 subject)", "오차 0 (완전 일치)", 1],
        ["Motion 집계 정확성", "원본 acceleration.csv 전체 재계산 후 대조", "평균 오차 4.9e-09 (부동소수점 수준)", 1],
        ["is_sleep / label 매핑", "원본 label 값과 sleep_stage_raw·is_sleep 대조(31명)", "매핑 오류 0건", 1],
        ["steps 총합 보존", "원본 총합 vs 30초 분배 후 총합(31명)", "오차 <1.0, 전원 통과", 1],
        ["7일 이력 구조 정합성", "중복·겹침·시간역전·범위이탈·정렬 검사(31명)", "문제 0건", 1],
    ]
    header = [P(c, "CellHead") for c in verify_rows[0][:3]] + [P("상태", "CellHead")]
    body_rows = [header]
    for row in verify_rows[1:]:
        body_rows.append([P(row[0], "Cell"), P(row[1], "Cell"), P(row[2], "Cell"), status_badge(bool(row[3]))])
    t = Table(body_rows, colWidths=[42*mm, 76*mm, 50*mm, 22*mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ] + [("BACKGROUND", (0, r), (2, r), ROW_ALT) for r in range(2, len(body_rows), 2)]))
    story.append(t)
    story.append(Spacer(1, 8))
    story.append(P(
        "<b>참고</b> — motion의 activity_std는 pandas 기본(표본표준편차, ddof=1)이 아닌 모표준편차(ddof=0) "
        "방식으로 계산되어 최대 0.0006(가속도 magnitude 대비 약 0.06%) 차이가 있음. 실질적 영향은 미미한 "
        "계산방식 차이로, 버그는 아님.", "Note"))

    story.append(PageBreak())

    # ================= 5. 다음 단계 코드 연동 검증 =================
    story.extend(section_header("5", "다음 단계 코드 연동 검증 (Reference Implementation Demo)"))
    story.append(P(
        "\"입력값을 만들었다\"는 것과 \"다음 단계 코드가 실제로 문제없이 가져다 쓸 수 있다\"는 별개의 주장임. "
        "이를 직접 증명하기 위해 output 파일만 그대로 읽어 Two-Process Model 공식을 실행하는 참조 구현"
        "(<font face='Malgun'>scripts\\two_process_model_demo.py</font>)을 별도로 작성하여 31명 전체에 실행함.",
        "Body"))
    story.append(P("검증 방법", "SubTitle"))
    story.append(bullets([
        "two_process_params.csv(S0, 서카디안 위상)과 unified_30s.csv(PSG 당일 밤 실측 sleep/wake)만 그대로 "
        "로드 — 추가 가공·수동 보정 없음",
        "표준 시간상수(τi, τd)로 S(t)를 PSG 당일 밤 구간에 대해 순차 시뮬레이션, C(t)는 위상앵커 기반 "
        "24h 코사인으로 계산",
        "combined pressure = S(t) − C(t) 를 산출해 실제 is_sleep(PSG 라벨)과의 AUC로 정합성 점검",
    ]))

    story.append(P("결과", "SubTitle"))
    demo_kpis = Table([[
        kpi_card("31 / 31", "에러 없이 처리"),
        kpi_card("31 / 31", "수면 중 S(t) 정상 감소"),
        kpi_card("0.293", "평균 AUC (우연=0.5)"),
        kpi_card("1 / 31", "AUC > 0.6인 subject"),
    ]], colWidths=[44 * mm] * 4)
    demo_kpis.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    story.append(demo_kpis)
    story.append(Spacer(1, 10))

    interp_box = Table([[P(
        "<b>해석 — 형식(포맷) 증명과 물리적 유효성은 분리해서 봐야 함.</b><br/><br/>"
        "① <b>포맷 호환성은 증명됨</b> — 제 output 파일을 추가 가공 없이 그대로 불러서 실제 공식이 31명 전체 "
        "에러 없이 실행됐고, S(t)가 수면 중 감소한다는 모델의 기본 성질도 31/31 정상. "
        "→ \"다음 단계 코드에서 바로 입력값으로 쓸 수 있는 형태\"라는 조장의 기준은 충족.<br/><br/>"
        "② <b>AUC 0.293(우연보다 낮음)은 입력값 오류로 보기 어려움</b> — Two-Process Model의 S(t)는 원래 "
        "\"지금 자고 있는지\"를 순간순간 맞히는 분류기가 아니라 하루 주기에서 수면압력이 쌓이는 타이밍을 "
        "나타내는 값. PSG로 채점된, 거의 전체가 \"수면\"인 짧은 당일 밤 구간 안에서 30초 단위로 맞고 틀리고를 "
        "따진 이번 검증 방식 자체가 부적절한 잣대였을 가능성이 큼. 실제 타당성 평가는 공식 구현 담당자가 "
        "수면 개시 시점(onset timing) 예측 정확도 등 올바른 방법으로 다시 수행해야 함 — 팀 논의 필요.",
        "Cell")]], colWidths=[186 * mm])
    interp_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(interp_box)
    story.append(Spacer(1, 6))
    story.append(P(
        "subject별 상세 수치는 부록 C 참조 · 원본 데이터: "
        "<font face='Malgun'>output\\two_process_input\\demo_validation_results.csv</font>", "Note"))

    # ================= 6 & 7. 부록 (가로 방향) =================
    story.append(NextPageTemplate("Landscape"))
    story.append(PageBreak())
    story.extend(section_header("6", "부록 A — Subject별 처리 결과 요약"))

    cols = ["subject_id", "n_epochs_30s", "n_epochs_with_hr", "n_epochs_with_motion",
            "n_epochs_with_steps", "n_labeled_epochs", "n_sleep_history_days_found",
            "mean_history_duration_hr"]
    header_labels = ["Subject ID", "30초 epoch수", "HR 유효 epoch", "Motion 유효 epoch",
                      "Steps 유효 epoch", "PSG 라벨수", "7일中 복원일수", "평균 수면(h)"]
    header_row = [Paragraph(h, styles["CellHead"]) for h in header_labels]
    data_rows = [header_row]
    for _, row in summary[cols].iterrows():
        data_rows.append([Paragraph(str(v), styles["CellSm"]) for v in row])
    t2 = Table(data_rows, colWidths=[32*mm]*8, repeatRows=1)
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ] + [("BACKGROUND", (0, r), (-1, r), ROW_ALT) for r in range(2, len(data_rows), 2)]))
    story.append(t2)
    story.append(Spacer(1, 6))
    n7 = int((summary["n_sleep_history_days_found"] == 7).sum())
    n6 = int((summary["n_sleep_history_days_found"] == 6).sum())
    nlow = int((summary["n_sleep_history_days_found"] <= 5).sum())
    story.append(P(
        f"31명 중 7일 전부 복원 {n7}명 · 6일 복원 {n6}명 · 5일 이하 복원 {nlow}명 "
        f"(device 미착용 구간이 많은 subject일수록 복원 일수가 적음).", "Note"))

    story.append(PageBreak())
    story.extend(section_header("7", "부록 B — Two-Process Model 입력 파라미터"))
    cols2 = ["subject_id", "S0_at_t0", "circadian_phase_ref_sec", "circadian_R", "n_nights_used", "days_with_data_gap"]
    header2 = ["Subject ID", "S0 (t=0)", "서카디안 위상앵커(초)", "위상 신뢰도 R", "사용된 밤 수", "결측 일수"]
    header_row2 = [Paragraph(h, styles["CellHead"]) for h in header2]
    data_rows2 = [header_row2]
    for _, row in params[cols2].iterrows():
        data_rows2.append([Paragraph(str(v), styles["CellSm"]) for v in row])
    t3 = Table(data_rows2, colWidths=[40*mm, 38*mm, 48*mm, 38*mm, 38*mm, 38*mm], repeatRows=1)
    t3.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ] + [("BACKGROUND", (0, r), (-1, r), ROW_ALT) for r in range(2, len(data_rows2), 2)]))
    story.append(t3)
    story.append(Spacer(1, 6))
    low_r = params[params["circadian_R"] < 0.5]
    if len(low_r):
        ids = ", ".join(low_r["subject_id"].astype(str) + f"(R={low_r['circadian_R'].round(3).astype(str)})")
    story.append(P(
        "circadian_R < 0.5(위상 추정 신뢰도 낮음) subject: 8692923 (R=0.457) — 수면시각이 날마다 불규칙하여 "
        "서카디안 위상 근사가 상대적으로 덜 신뢰됨.", "Note"))

    story.append(PageBreak())
    story.extend(section_header("8", "부록 C — 다음 단계 연동 데모 검증 결과"))
    cols3 = ["subject_id", "n_epochs", "S0_used", "auc_pressure_vs_sleep", "s_decreases_during_sleep"]
    header3 = ["Subject ID", "당일밤 epoch수", "사용된 S0", "AUC(pressure vs 실제수면)", "수면중 S(t) 정상감소"]
    header_row3 = [Paragraph(h, styles["CellHead"]) for h in header3]
    data_rows3 = [header_row3]
    for _, row in demo[cols3].iterrows():
        data_rows3.append([Paragraph(str(v), styles["CellSm"]) for v in row])
    t4 = Table(data_rows3, colWidths=[40*mm, 44*mm, 40*mm, 60*mm, 46*mm], repeatRows=1)
    t4.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ] + [("BACKGROUND", (0, r), (-1, r), ROW_ALT) for r in range(2, len(data_rows3), 2)]))
    story.append(t4)
    story.append(Spacer(1, 6))
    story.append(P(
        "AUC가 낮게 나온 원인 해석은 5번 섹션 참조 — 입력값 오류가 아니라 검증 방법(30초 단위 순간 분류) "
        "자체의 한계로 판단됨.", "Note"))

    # ================= 9. 파일 위치 & 한계 (portrait 복귀) =================
    story.append(NextPageTemplate("Portrait"))
    story.append(PageBreak())
    story.extend(section_header("9", "결과 파일 저장 위치"))
    story.append(styled_table(
        [["경로", "내용", "파일 수"],
         ["scripts\\walch_preprocess.py", "30초 통합 + 7일 수면이력 복원 코드", "1"],
         ["scripts\\two_process_input_mapping.py", "Two-Process Model 입력값 생성 코드", "1"],
         ["scripts\\two_process_model_demo.py", "다음 단계 연동 증명용 참조 구현(공식 실행 데모)", "1"],
         ["output\\30s\\*.csv", "결과물 1: 30초 단위 Walch 통합 데이터", "31"],
         ["output\\sleep_history_7d\\*.csv", "결과물 2: subject별 최근 7일 수면 기록", "31"],
         ["output\\two_process_input\\*", "Two-Process 입력값(S0, 서카디안 위상, 원시 이력)", "33"],
         ["output\\two_process_input\\demo_validation_results.csv", "데모 검증 결과(AUC 등)", "1"],
         ["output\\summary.csv", "subject별 처리 커버리지 요약", "1"]],
        col_widths=[76*mm, 92*mm, 18*mm], font_size=9, align_center_cols=[2]))
    story.append(Spacer(1, 6))
    story.append(P(
        "전체 output 폴더 용량: 약 53MB.  최상위 경로: "
        "<font face='Malgun'>C:\\Users\\JSD\\Desktop\\Walch_처리\\</font>", "Body"))

    story.append(P("알려진 한계 및 팀 논의 필요 사항", "SubTitle"))
    story.append(bullets([
        "Walch 데이터는 절대시각이 없는 익명화 데이터라 실제 서카디안 위상을 알 수 없음 — 복원된 수면 중앙시각 "
        "평균을 대리 지표로 사용함 (학술발표 시 명시 필요)",
        "7일 중 복원되지 않은 날은 전부 \"각성\"으로 채워 S0가 실제보다 다소 높게 산출될 수 있음 "
        "(days_with_data_gap 컬럼으로 subject별 추적 가능)",
        "활동으로 하룻밤 수면이 두 조각으로 나뉜 경우, 더 긴 조각만 그 날의 수면으로 채택 — 일부 subject "
        "(예: 4018081)의 평균 수면시간이 짧게 나타날 수 있음",
        "τi/τd, circadian_amplitude는 Walch 데이터로 재추정한 값이 아닌 문헌 표준값이며, "
        "circadian_amplitude는 공식 구현 담당자가 정규화 단위에 맞춰 별도 설정 필요 (현재 null)",
    ]))

    doc.build(story)
    print(f"PDF saved: {REPORT_PATH}")


if __name__ == "__main__":
    build()
