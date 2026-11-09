"""Three everyday SleepWell steps over the existing immutable model engines."""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, ttk

import pandas as pd

from .app import tree
from .data_quality import load_project_quality


def experiment_slice(frame, *, people=3, nights=30, cohort='all', split='train'):
    """Explicit, deterministic exploratory subset; never silently repair observations."""
    if not 1 <= people <= 10 or not 1 <= nights or people * nights > 365:
        raise ValueError('참여자 1–10명, 참여자 수 × 밤 수는 365 이하여야 합니다. / Maximum 10 participants and 365 nights.')
    data = frame.copy()
    required = {'participant_id': 'subject_id', 'cutoff': 'cutoff', 'sleep_start': 'bed',
                'sleep_end': 'wake', 'observed_at': 'observed_at'}
    for key, alias in required.items():
        if key not in data:
            if alias not in data:
                raise ValueError(f'필수 열 누락 / Missing required column: {key}')
            data[key] = data[alias]
    if data['participant_id'].isna().any():
        raise ValueError('참여자 식별자가 비어 있습니다. / Missing participant identifier.')
    if 'cohort' in data:
        data['participant_id'] = data['cohort'].astype(str) + '.' + data['participant_id'].astype(str)
        if cohort != 'all':
            data = data.loc[data['cohort'].astype(str).str.lower() == cohort.lower()]
    elif cohort != 'all':
        raise ValueError('코호트 열이 없습니다. / Cohort column is missing.')
    applied_split = split if 'split' in data else 'not_available'
    if 'split' in data and split != 'all':
        data = data.loc[data['split'].astype(str).str.lower() == split]
    if data.empty:
        raise ValueError('선택한 범위에 데이터가 없습니다. / No data in the selected scope.')
    ids = sorted(data['participant_id'].astype(str).unique())[:people]
    data = data.loc[data['participant_id'].astype(str).isin(ids)].copy()
    # The engine remains responsible for strict timezone and outcome validation.
    data['_order'] = pd.to_datetime(data['cutoff'], utc=True, errors='raise')
    data = data.sort_values(['participant_id', '_order'], kind='stable').groupby('participant_id', sort=False).head(nights)
    selected = data[list(required)].copy()
    composition = (data.groupby('cohort')['participant_id'].nunique().astype(int).to_dict()
                   if 'cohort' in data else {})
    return selected, {'source_rows': len(frame), 'rows': len(selected), 'participants': len(ids),
                      'cohort': cohort, 'split': applied_split, 'max_nights_per_participant': nights,
                      'actual_cohort_participants': composition,
                      'selection_rule': 'sorted_participant_ids_then_earliest_nights',
                      'purpose': 'exploratory_model_experiment_not_independent_validation'}


class SimpleWorkflowMixin:
    def sw(self, ko, en):
        return en if self.lang == 'en' else ko

    def workflow_label(self, page):
        return {'data': self.sw('데이터 품질', 'Data quality'),
                'experiments': self.sw('모델 실험 · 시각화', 'Experiment & visualize'),
                'reports': self.sw('RAG 보고서', 'Evidence report')}[page]

    def simple_note(self, parent, ko, en):
        label = ttk.Label(parent, text=self.sw(ko, en), wraplength=980,
                          justify='left', style='AcademicNote.TLabel')
        label.pack(fill='x', pady=(3, 8))
        parent.bind('<Configure>', lambda event: label.configure(wraplength=max(280, event.width - 25)), add='+')
        return label

    def simple_button(self, parent, ko, en, callback, *, primary=False):
        button = ttk.Button(parent, text=self.sw(ko, en), command=lambda: self.safe(callback),
                            style='Primary.TButton' if primary else 'TButton')
        button.pack(side='left', padx=(0, 8), pady=4)
        return button

    def load_existing_cohorts(self):
        from scripts.build_cmi_mesa_db import DEFAULT_SOURCE
        source = DEFAULT_SOURCE
        if not source.is_file():
            source = filedialog.askopenfilename(parent=self.root, title=self.sw('기존 수면 자료 선택', 'Choose existing sleep data'),
                                               filetypes=[('Data', '*.csv *.xlsx *.json')])
        if source:
            self.import_data(source, synthetic=False)

    def build_quality(self, parent):
        self.simple_note(parent, '데이터의 결측·중복·시간 오류를 먼저 확인하세요. 원자료는 자동으로 수정하지 않습니다.',
                         'Check missing values, duplicates, and time errors. Original data is never automatically changed.')
        row = ttk.Frame(parent); row.pack(fill='x')
        self.simple_button(row, '파일 불러오기', 'Import file', self.import_data)
        self.simple_button(row, '기존 CMI·MESA 자료', 'Existing CMI/MESA data', self.load_existing_cohorts)
        self.simple_button(row, '모델 실험으로 →', 'Experiment →', lambda: self.navigate('experiments'), primary=True)
        item = self.project.state.get('input')
        if not item:
            self.simple_note(parent, '아직 데이터가 없습니다. 파일을 불러오거나 기존 CMI·MESA 자료를 선택하세요.',
                             'No data yet. Import a file or choose the existing CMI/MESA dataset.')
            return
        frame, summary = load_project_quality(self.project)
        self.quality_summary = summary
        badge = self.sw('합성 예제', 'Synthetic example') if item.get('synthetic') else self.sw('기존 자료', 'Existing data')
        self.simple_note(parent, f"{item['source_name']} · {badge}", f"{item['source_name']} · {badge}")
        values = self.sw(
            f"{summary['rows']:,}행 · 참여자 {summary['participants'] if summary['participants'] is not None else '확인 불가'}명 · "
            f"결측 {summary['missing_cells']:,}개 ({summary['missing_cell_fraction']:.1%}) · 중복 행 {summary['exact_duplicate_rows']:,}개",
            f"{summary['rows']:,} rows · {summary['participants']} participants · {summary['missing_cells']:,} missing cells "
            f"({summary['missing_cell_fraction']:.1%}) · {summary['exact_duplicate_rows']:,} duplicate rows")
        ttk.Label(parent, text=values, font=('Malgun Gothic', 12, 'bold'), wraplength=960).pack(fill='x', pady=12)
        findings = summary['findings']
        descriptions = {
            'missing_cells': ('결측값이 있는 열을 아래 표에서 확인하세요.', 'Inspect columns with missing values below.'),
            'exact_duplicate_rows': ('동일한 행이 반복됩니다.', 'Identical rows are repeated.'),
            'duplicate_participant_cutoff': ('같은 참여자·예측 시점이 중복됩니다.', 'Participant/cutoff keys are duplicated.'),
            'missing_required_columns': ('수면 모델에 필요한 열이 없습니다.', 'Required sleep model columns are missing.'),
            'missing_required_values': ('수면 모델의 필수 값이 비어 있습니다.', 'Required sleep model values are missing.'),
            'invalid_timestamps': ('비어 있거나 해석할 수 없는 시각이 있습니다.', 'Missing or invalid timestamps.'),
            'nonpositive_sleep_intervals': ('기상 시각이 취침 시각보다 늦지 않은 기록이 있습니다.', 'Sleep end is not later than sleep start.'),
            'observed_before_sleep_end': ('관측 시각이 기상보다 이릅니다.', 'Observation precedes sleep end.'),
            'history_below_recent7_window': ('7밤 미만 기록을 가진 참여자가 있습니다. 모델 입력에서 자동 제외하지 않습니다.', 'Some participants have fewer than 7 nights. They are not automatically excluded.'),
            'empty_dataset': ('데이터가 비어 있습니다.', 'The dataset is empty.'),
            'invalid_participant_id': ('참여자 ID 형식을 확인하세요. 영문·숫자·점·밑줄·하이픈을 사용할 수 있습니다.', 'Check participant IDs: ASCII letters, digits, period, underscore, and hyphen are allowed.'),
            'cutoff_not_2000_kst': ('예측 기준 시각은 한국시간 20시여야 합니다.', 'Prediction cutoff must be 20:00 Korea time.'),
            'onset_outside_supported_horizon': ('취침 시각이 예측 기준 후 0–960분 범위를 벗어났습니다.', 'Sleep onset is outside 0–960 minutes after cutoff.'),
            'sleep_duration_outside_30_960_min': ('수면 길이가 모델 입력 범위인 30–960분을 벗어났습니다.', 'Sleep duration is outside the supported 30–960 minutes.'),
            'wake_after_cutoff_1440_min': ('기상 시각이 예측 기준 후 24시간을 넘습니다.', 'Wake time is more than 24 hours after cutoff.'),
        }
        for finding in findings[:8]:
            text = descriptions.get(finding['code'], (finding['detail'], finding['detail']))
            ttk.Label(parent, text=f"• {self.sw(*text)} ({finding['count']})", wraplength=960).pack(fill='x', pady=2)
        if not findings:
            self.simple_note(parent, '이 품질 점검에서 발견된 문제는 없습니다.', 'No issues found by these quality checks.')
        self.simple_note(parent, '열별 품질', 'Column quality')
        self.quality_tree = tree(parent, ['name', 'missing', 'unique', 'type'],
            [self.sw('열', 'Column'), self.sw('결측', 'Missing'), self.sw('고유값', 'Unique'), self.sw('자료형', 'Type')], height=7)
        for col in summary['columns']:
            self.quality_tree.insert('', 'end', values=(col['name'], f"{col['missing']} ({col['missing_fraction']:.1%})", col['unique'], col['dtype']))
        self.simple_note(parent, '원자료 미리보기 · 처음 5행', 'Source preview · first 5 rows')
        preview = tree(parent, list(frame.columns), list(frame.columns), height=5)
        for values in frame.head(5).fillna('').itertuples(index=False, name=None):
            preview.insert('', 'end', values=values)

    def build_experiments(self, parent):
        self.simple_note(parent, '기존 자료에서 선택한 범위로 gradual_v1 모델을 실행합니다. 진행 상태와 완료된 성능을 이 화면에서 확인합니다.',
                         'Run gradual_v1 on an explicit subset of existing data. Follow progress and completed performance here.')
        setup = ttk.LabelFrame(parent, text=self.sw('실험 범위', 'Experiment scope'), padding=10)
        setup.pack(fill='x', pady=(0, 8))
        item = self.project.state.get('input')
        ttk.Label(setup, text=item['source_name'] if item else self.sw('데이터를 먼저 불러오세요.', 'Import data first.')).pack(anchor='w')
        defaults = {'experiment_people': '3', 'experiment_nights': '30', 'experiment_cohort': 'all', 'experiment_split': 'train'}
        for key, default in defaults.items():
            if not self.var(key).get(): self.var(key).set(default)
        row = ttk.Frame(setup); row.pack(fill='x', pady=6)
        for key, ko, en, options in [
            ('experiment_cohort', '자료', 'Data', ['all', 'cmi', 'mesa']),
            ('experiment_split', '분할', 'Split', ['train', 'validation', 'test', 'all']),
            ('experiment_people', '참여자 수', 'Participants', None),
            ('experiment_nights', '1인당 밤 수', 'Nights/person', None),
        ]:
            ttk.Label(row, text=self.sw(ko, en)).pack(side='left', padx=(0, 4))
            widget = ttk.Combobox(row, textvariable=self.var(key), values=options, state='readonly', width=9) if options else ttk.Spinbox(row, textvariable=self.var(key), from_=1, to=10 if key.endswith('people') else 365, width=5)
            widget.pack(side='left', padx=(0, 14))
        self.simple_note(setup, '참여자 ID 순으로 선택하고 각 참여자의 처음 N밤을 사용합니다. 분할이 없는 파일은 전체가 대상이며 최대 10명·365밤입니다.',
                         'Select sorted participant IDs and their earliest N nights. Files without split labels use all rows. Maximum 10 people and 365 nights.')
        row = ttk.Frame(setup); row.pack(fill='x')
        ttk.Label(row, text=self.sw('비교할 모델', 'Models to compare')).pack(side='left', padx=(0, 12))
        for cohort in ['cmi', 'mesa']:
            ttk.Checkbutton(row, text=cohort.upper(), variable=self.var(cohort), onvalue='1', offvalue='0').pack(side='left', padx=(0, 14))
        self.simple_start_button = self.simple_button(row, '실험 실행', 'Run experiment', self.start_simple_experiment, primary=True)
        self.simple_cancel_button = self.simple_button(row, '중단', 'Stop', self.stop_simple_experiment)
        self.simple_progress = ttk.Progressbar(parent, maximum=100)
        self.simple_progress.pack(fill='x', pady=3)
        self.simple_progress_label = ttk.Label(parent, wraplength=960)
        self.simple_progress_label.pack(fill='x', pady=(0, 8))
        self.refresh_simple_progress()
        choices = [r for r in self.records() if r['status'] == 'completed' and r.get('result', {}).get('summary')]
        self.simple_result_choices = choices
        row = ttk.Frame(parent); row.pack(fill='x', pady=6)
        ttk.Label(row, text=self.sw('저장된 결과', 'Saved result')).pack(side='left', padx=(0, 8))
        self.simple_result_combo = ttk.Combobox(row, state='readonly', width=55,
            values=[f"{r['name']} · {r['created'][:10]} · {r['id'][:6]}" for r in choices])
        self.simple_result_combo.pack(side='left', fill='x', expand=True)
        self.simple_result_combo.bind('<<ComboboxSelected>>', lambda event: self.safe(self.select_simple_result))
        self.simple_button(row, '보고서로 →', 'Report →', lambda: self.navigate('reports'))
        if not choices:
            self.simple_note(parent, '실험이 완료되면 이곳에 성능 그래프와 표가 표시됩니다.', 'Performance charts and tables appear when the experiment finishes.')
            return
        index = next((i for i, record in enumerate(choices) if record['id'] == self.selected_run),
                     len(choices) - 1 if not self.selected_run else None)
        if index is None:
            self.simple_result_combo.set(self.sw('현재 실행의 완료 결과 없음 · 이전 결과는 직접 선택',
                                                  'Current run has no completed result · select history explicitly'))
            self.simple_note(parent, '현재 실행이 완료되면 그래프가 표시됩니다. 실패하거나 중단된 실행에는 성능을 표시하지 않습니다.',
                             'Charts appear when the current run completes. Failed or stopped runs have no performance result.')
            return
        self.simple_result_combo.current(index)
        self.selected_run = choices[index]['id']
        self.build_simple_comparison(parent, choices[index]['result'])

    def build_simple_comparison(self, parent, result):
        from .academic_plotting import model_label
        from .result_adapter import digest
        record = self.simple_result_choices[self.simple_result_combo.current()]
        saved_selection = self.project.state.get('comparison_selections', {}).get(record['id'])
        if saved_selection:
            if saved_selection.get('run_id') != record['id'] or saved_selection.get('result_hash') != digest(record):
                raise ValueError('comparison_selection_run_mismatch')
            for key in ('target', 'averaging', 'tolerance'):
                self.var('comparison_' + key).set(str(saved_selection[key]))
        controls = ttk.LabelFrame(parent, text=self.sw('같은 자료에서 모델 비교', 'Compare models on the same data'), padding=8)
        controls.pack(fill='x', pady=5)
        row = ttk.Frame(controls); row.pack(fill='x')
        ttk.Label(row, text=self.sw('예측 대상', 'Target')).pack(side='left', padx=(0, 5))
        self.simple_target_combo = ttk.Combobox(row, state='readonly', width=18,
            values=[self.sw('취침 시각', 'Sleep onset'), self.sw('기상 시각', 'Wake time'), self.sw('취침–기상 구간', 'Sleep interval')])
        targets = ['onset', 'wake', 'interval']
        self.simple_target_combo.current(targets.index(self.var('comparison_target').get()) if self.var('comparison_target').get() in targets else 0)
        self.simple_target_combo.pack(side='left', padx=(0, 12))
        ttk.Label(row, text=self.sw('집계', 'Averaging')).pack(side='left', padx=(0, 5))
        self.simple_averaging_combo = ttk.Combobox(row, state='readonly', width=23,
            values=[self.sw('참여자 동일 가중 · 기본', 'Equal participant weight'), self.sw('밤 동일 가중', 'Equal night weight')])
        self.simple_averaging_combo.current(1 if self.var('comparison_averaging').get() == 'night_equal' else 0)
        self.simple_averaging_combo.pack(side='left', padx=(0, 12))
        ttk.Label(row, text=self.sw('허용 오차(분)', 'Tolerance (min)')).pack(side='left', padx=(0, 5))
        self.simple_tolerance_combo = ttk.Combobox(row, state='readonly', width=5, values=['15', '30', '60'])
        self.simple_tolerance_combo.set(self.var('comparison_tolerance').get() if self.var('comparison_tolerance').get() in {'15','30','60'} else '30')
        self.simple_tolerance_combo.pack(side='left')
        for combo in (self.simple_target_combo, self.simple_averaging_combo, self.simple_tolerance_combo):
            combo.bind('<<ComboboxSelected>>', lambda event: self.safe(self.refresh_simple_result))
        row = ttk.Frame(controls); row.pack(fill='x', pady=(5, 0))
        models = list(dict.fromkeys(str(item['model_id']) for item in
            [*result.get('summary', []), *(result.get('raw') or {}).get('predictions', [])] if item.get('model_id')))
        selected_models = saved_selection['model_ids'] if saved_selection else models
        if any(model not in models for model in selected_models):
            raise ValueError('comparison_selection_unknown_model')
        self.simple_model_vars = {model: tk.BooleanVar(self.root, value=model in selected_models) for model in models}
        self.simple_model_button = ttk.Menubutton(row)
        menu = tk.Menu(self.simple_model_button, tearoff=False)
        menu.add_command(label=self.sw('전체 선택', 'Select all'), command=lambda: self.safe(lambda: self.set_simple_models(True)))
        menu.add_command(label=self.sw('전체 해제', 'Clear selection'), command=lambda: self.safe(lambda: self.set_simple_models(False)))
        menu.add_separator()
        for model, variable in self.simple_model_vars.items():
            menu.add_checkbutton(label=model_label(model, self.lang), variable=variable,
                                 command=lambda: self.safe(self.refresh_simple_result))
        self.simple_model_button.configure(menu=menu)
        self.simple_model_button.pack(side='left', padx=(0, 12))
        self.simple_chart_export_button = self.simple_button(row, '현재 그래프 저장', 'Save current chart', self.export_plot)
        self.simple_table_export_button = self.simple_button(row, '비교 지표 저장', 'Save comparison metrics', self.export_simple_comparison_table)
        self.simple_result_note = ttk.Label(parent, wraplength=960, style='AcademicNote.TLabel')
        self.simple_result_note.pack(fill='x', pady=5)
        parent.bind('<Configure>', lambda event: self.simple_result_note.configure(wraplength=max(280, event.width - 25)) if self.simple_result_note.winfo_exists() else None, add='+')
        self.comparison_tabs = ttk.Notebook(parent)
        self.comparison_tabs.pack(fill='both', expand=True, pady=6)
        self.comparison_tabs.enable_traversal()
        self.comparison_frames = {}
        self.simple_tab_notes = {}
        tab_info = [
            ('mae', 'MAE', 'MAE', '평균 절대 오차(분) · 작을수록 좋습니다.', 'Mean absolute error (min): lower is better.'),
            ('rmse', 'RMSE', 'RMSE', '큰 오차에 더 민감한 지표(분) · 작을수록 좋습니다.', 'Root mean squared error (min): penalizes large errors; lower is better.'),
            ('bias', '오차 방향', 'Bias', '예측−실제의 평균(분). 음수는 이르게, 양수는 늦게 예측합니다. 구간은 음수=짧게, 양수=길게입니다.', 'Mean predicted−observed (min). Negative means earlier/shorter; positive means later/longer.'),
            ('tolerance', '허용오차 내 비율', 'Within tolerance', '절대 오차가 선택한 분 이내인 비율입니다. 15·30·60분은 탐색 기준이며 임상 기준이나 분류 정확도가 아닙니다.', 'Fraction within the chosen tolerance. 15/30/60 minutes are exploratory choices, not clinical cutoffs or classification accuracy.'),
            ('predictions', '실제·예측', 'Observed vs predicted', '모델별 같은 축에서 비교합니다. 점이 대각선에 가까울수록 실제와 예측이 일치합니다.', 'Shared axes by model. Points near the diagonal agree with observations.'),
            ('errors', '오차 분포', 'Error distribution', '각 밤의 예측−실제 오차를 표시합니다. 상자는 분포이며 신뢰구간이 아닙니다.', 'Signed errors for each night. Boxes describe the distribution, not confidence intervals.'),
            ('participants', '참여자별', 'Participants', '각 참여자의 MAE를 표시합니다. 소수 참여자의 탐색 결과로 통계적 우월성을 판단하지 않습니다.', 'MAE for each participant. Small exploratory samples do not establish statistical superiority.'),
            ('classification', 'ROC·분류 지표', 'ROC & classification', '', ''),
        ]
        for key, ko, en, note_ko, note_en in tab_info:
            frame = ttk.Frame(self.comparison_tabs, padding=8)
            self.comparison_tabs.add(frame, text=self.sw(ko, en))
            self.comparison_frames[key] = frame
            if key != 'classification':
                self.simple_tab_notes[key] = self.simple_note(frame, note_ko, note_en)
        guide = self.comparison_frames['classification']
        self.simple_note(guide,
            '현재 모델의 ROC-AUC·민감도·특이도·분류 정확도: 적용 대상 아님',
            'ROC-AUC, sensitivity, specificity and classification accuracy: not applicable to the current model.')
        self.simple_note(guide,
            'SleepWell은 취침·기상 시각이라는 연속값을 예측합니다. MAE·RMSE와 허용오차 내 비율로 비교하세요.\n\n'
            'ROC-AUC에는 사전에 정의한 분류 정답과 연속적인 예측 점수가 필요합니다. 민감도·특이도·정확도에는 정답 라벨과 분류 결정이 필요합니다. 현재 결과에는 이러한 정보가 없어 계산하지 않습니다.\n\n'
            '수면 시각을 임의로 좋음/나쁨으로 바꾸거나 시간 오차를 분류 정확도로 표시하지 않습니다.',
            'SleepWell predicts continuous sleep onset and wake times. Compare MAE, RMSE and the fraction within tolerance.\n\n'
            'ROC-AUC needs predefined class labels and continuous prediction scores. Sensitivity, specificity and accuracy need true labels and classification decisions. These are absent from the current results.\n\n'
            'We do not invent good/bad sleep labels or relabel timing errors as classification accuracy.')
        self.simple_note(guide, '정의 근거: scikit-learn 공식 회귀 지표·ROC-AUC·classification_report 문서.',
                         'Definitions: official scikit-learn regression metrics, ROC-AUC and classification_report documentation.')
        preferred = self.var('comparison_tab').get()
        self.comparison_tabs.select(self.comparison_frames.get(preferred, self.comparison_frames['mae']))
        self.simple_metric_tree = tree(parent, ['model', 'mae', 'rmse', 'bias', 'within', 'n', 'people'],
            [self.sw('모델', 'Model'), 'MAE (min)', 'RMSE (min)', self.sw('평균오차(분)', 'Bias (min)'),
             self.sw('허용오차 내(%)', 'Within tolerance (%)'), self.sw('공통 밤', 'Common nights'), self.sw('참여자', 'Participants')], height=5)
        self.simple_metric_tree.column('model', width=220)
        for column in ['mae', 'rmse', 'bias', 'within', 'n', 'people']:
            self.simple_metric_tree.column(column, width=100, minwidth=65)
        self.comparison_tabs.bind('<<NotebookTabChanged>>', lambda event: self.safe(self.refresh_simple_result))
        self.refresh_simple_result()

    def start_simple_experiment(self):
        if self.runner.busy:
            raise ValueError(self.sw('실행 중인 실험을 완료하거나 중단하세요.', 'Finish or stop the current experiment first.'))
        frame, quality = load_project_quality(self.project)
        cohorts = [key for key in ['cmi', 'mesa'] if self.var(key).get() == '1']
        if not cohorts: raise ValueError(self.sw('비교할 모델을 선택하세요.', 'Choose a model.'))
        selected, scope = experiment_slice(frame, people=int(self.var('experiment_people').get() or 3),
            nights=int(self.var('experiment_nights').get() or 30), cohort=self.var('experiment_cohort').get() or 'all',
            split=self.var('experiment_split').get() or 'train')
        data = selected.to_csv(index=False).encode('utf-8-sig')
        from .academic_backend import vendor_module
        vendor_module('research_lab')._validate_payload({
            'schema_version': 1, 'mode': 'real', 'cohorts': cohorts, 'csv_text': data.decode('utf-8-sig')})
        relative, digest = self.project.store_input_snapshot(data)
        source = self.project.state['input']
        scope['source_sha256'] = source['sha256']
        name = self.sw('모델 실험', 'Model experiment') + f" · {scope['cohort']} / {scope['split']} · {scope['participants']}명 {scope['rows']}밤"
        config = {'name': name, 'mode': 'personalization', 'cohorts': cohorts, 'max_seconds': 600,
                  'input_mode': 'real', 'input_snapshot': relative, 'data_hash': digest,
                  'source_name': source['source_name'], 'input_synthetic': bool(source.get('synthetic')),
                  'selection': scope}
        self.selected_run = self.runner.submit(config)
        self.save()
        self.navigate('experiments')

    def stop_simple_experiment(self):
        records = [self.runner.active['record']] if self.runner.active else list(self.runner.pending)
        for record in records: self.runner.cancel(record['id'])
        self.refresh_simple_progress()

    def refresh_simple_progress(self):
        if not getattr(self, 'simple_progress', None) or not self.simple_progress.winfo_exists(): return
        active = self.runner.active['record'] if self.runner.active else (self.runner.pending[0] if self.runner.pending else None)
        self.simple_start_button.configure(state='disabled' if self.runner.busy else 'normal')
        self.simple_cancel_button.configure(state='normal' if self.runner.busy else 'disabled')
        record = active or getattr(self, 'simple_last_event', None)
        if not record:
            self.simple_progress_label.configure(text=self.sw('실행 준비됨 · 그래프는 계산 완료 후 갱신됩니다.', 'Ready · charts update after computation completes.'))
            return
        progress = record.get('progress') or {}
        self.simple_progress['value'] = 100 if record['status'] == 'completed' else float(progress.get('fraction', 0)) * 100
        stage = self.t(record.get('stage', record['status']))
        status = self.t(record['status'])
        detail = record.get('error', '')
        self.simple_progress_label.configure(text=f"{status} · {stage} · {record.get('duration', 0):.1f}s" + (f" · {detail}" if detail else ''))

    def select_simple_result(self):
        index = self.simple_result_combo.current()
        if index >= 0:
            self.selected_run = self.simple_result_choices[index]['id']
            self.navigate('experiments')

    def refresh_simple_result(self):
        from .academic_plotting import model_label
        from .comparison_metrics import comparison_data
        from .comparison_plotting import make_comparison_figure
        from .canvas import ResearchCanvas
        if self.page != 'experiments' or not getattr(self, 'comparison_tabs', None) or not self.comparison_tabs.winfo_exists():
            return
        index = self.simple_result_combo.current()
        if index < 0: return
        record = self.simple_result_choices[index]
        self.selected_run = record['id']
        if self.plot_canvas:
            self.plot_canvas.get_tk_widget().destroy()
            self.plot_canvas = None
        if self.figure:
            self.figure.clear()
            self.figure = None
        self.simple_metric_tree.delete(*self.simple_metric_tree.get_children())
        self.simple_comparison_data = None
        self.simple_chart_export_button.configure(state='disabled')
        self.simple_table_export_button.configure(state='disabled')
        models = [model for model, variable in self.simple_model_vars.items() if variable.get()]
        self.simple_model_button.configure(text=self.sw(f'비교 모델 선택 ({len(models)}/{len(self.simple_model_vars)}) ▾',
                                                        f'Models ({len(models)}/{len(self.simple_model_vars)}) ▾'))
        target = ['onset', 'wake', 'interval'][max(0, self.simple_target_combo.current())]
        averaging = ['participant_equal', 'night_equal'][max(0, self.simple_averaging_combo.current())]
        tolerance = int(self.simple_tolerance_combo.get())
        kind = next(key for key, frame in self.comparison_frames.items() if str(frame) == self.comparison_tabs.select())
        for key, value in [('target', target), ('averaging', averaging), ('tolerance', str(tolerance)), ('tab', kind)]:
            self.var('comparison_' + key).set(value)
        data = comparison_data(record['result'], model_ids=models, target=target, averaging=averaging, tolerance=tolerance)
        self.simple_comparison_data = data
        from .result_adapter import digest
        self.project.state.setdefault('comparison_selections', {})[record['id']] = {
            'run_id': record['id'], 'result_hash': digest(record), 'model_ids': list(models),
            'target': target, 'averaging': averaging, 'tolerance': tolerance,
        }
        synthetic = bool(record['config'].get('input_synthetic'))
        scope = record['config'].get('selection', {})
        text = self.sw('합성 자료 실험', 'Synthetic experiment') if synthetic else self.sw('기존 자료 탐색 실험', 'Exploratory existing-data experiment')
        if scope:
            actual = ', '.join(f"{key}: {value}" for key, value in scope.get('actual_cohort_participants', {}).items())
            text += f" · {scope['split']} · {scope['rows']} rows" + (f" · {actual} participants" if actual else '')
        text += self.sw(f" · 비교 공통 {data['n_nights']}밤 / {data['n_participants']}명 · 독립 검증 성능이 아닙니다.",
                        f" · Common comparison: {data['n_nights']} nights / {data['n_participants']} participants · Not independent validation performance.")
        if averaging == 'participant_equal':
            text += self.sw('\n참여자별 지표를 먼저 구한 뒤 같은 가중치로 평균합니다. RMSE도 참여자별 RMSE의 평균입니다.',
                            '\nMetrics are averaged equally across participants, including the mean of participant RMSEs.')
        else:
            text += self.sw('\n모든 공통 밤에 같은 가중치를 부여합니다. 기록이 많은 참여자의 영향이 큽니다.',
                            '\nAll common nights receive equal weight; participants with more nights contribute more.')
        excluded = ', '.join(f"{model_label(model, self.lang)} {count['invalid_pairs'] + count['outside_common_nights']}"
            for model, count in data.get('excluded_counts', {}).items()
            if count['invalid_pairs'] or count['outside_common_nights'])
        if excluded:
            text += self.sw('\n비교에서 제외된 기록: ', '\nRecords excluded from comparison: ') + excluded
        warning_labels = {
            'Prediction rows are unavailable; summary values were not substituted.': '원시 예측 기록이 없어 이 비교를 계산할 수 없습니다.',
            'No models were selected.': '비교 모델을 하나 이상 선택하세요.',
            'At least one selected model has no valid prediction pairs.': '선택한 모델 중 유효한 예측·실제값 쌍이 없는 모델이 있습니다.',
            'The selected models have no common valid prediction nights.': '선택한 모델에 공통으로 비교할 수 있는 밤이 없습니다.',
            'Some predictions were excluded so every model uses identical valid nights.': '모든 모델을 동일한 유효 기록에서 비교하기 위해 일부 기록을 제외했습니다.',
        }
        warnings = [warning_labels.get(warning, warning) if self.lang == 'ko' else warning
                    for warning in data.get('warnings', []) if not warning.startswith('ROC-AUC,')]
        if warnings:
            text += '\n' + ' / '.join(warnings)
        self.simple_result_note.configure(text=text)
        for metric in sorted(data['rows'], key=lambda row: (row['mae'], row['model_id'])):
            self.simple_metric_tree.insert('', 'end', values=(model_label(metric['model_id'], self.lang),
                *[f"{metric[key]:.2f}" for key in ['mae', 'rmse', 'bias']], f"{100 * metric['within_tolerance']:.1f}",
                metric['n_nights'], metric['n_participants']))
        self.simple_table_export_button.configure(state='normal' if data['available'] else 'disabled')
        if kind == 'classification':
            return
        self.plot_host = self.comparison_frames[kind]
        self.figure = make_comparison_figure(data, kind=kind, lang=self.lang)
        self.plot_canvas = ResearchCanvas(self.figure, master=self.plot_host)
        self.plot_canvas.draw()
        self.plot_canvas.get_tk_widget().pack(fill='both', expand=True)
        self.simple_chart_export_button.configure(state='normal' if data['available'] else 'disabled')

    def set_simple_models(self, selected):
        for variable in self.simple_model_vars.values():
            variable.set(selected)
        self.refresh_simple_result()

    def export_simple_comparison_table(self):
        data = getattr(self, 'simple_comparison_data', None)
        if not data or not data['available']:
            raise ValueError(self.sw('비교할 모델과 유효한 예측 기록이 필요합니다.', 'Select models with valid predictions.'))
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension='.csv',
            initialfile='SleepWell_model_comparison.csv', filetypes=[('CSV', '*.csv'), ('Excel', '*.xlsx')])
        if not path:
            return
        frame = pd.DataFrame(data['rows'])
        frame['within_tolerance_percent'] = 100 * frame.pop('within_tolerance')
        frame.rename(columns={'mae': 'mae_min', 'rmse': 'rmse_min', 'bias': 'bias_min'}, inplace=True)
        frame['run_id'] = self.selected_run
        frame['target'] = data['target']
        frame['averaging'] = data['averaging']
        frame['tolerance_min'] = data['tolerance']
        frame['comparison_scope'] = 'common_participant_cutoff_selected_models'
        for key in ['invalid_pairs', 'outside_common_nights']:
            frame[key] = frame['model_id'].map(lambda model: data['excluded_counts'][model][key])
        frame['excluded_records'] = frame['invalid_pairs'] + frame['outside_common_nights']
        if path.lower().endswith('.xlsx'):
            frame.to_excel(path, index=False)
        else:
            frame.to_csv(path, index=False, encoding='utf-8-sig')
        self.status.set(self.t('saved_file') + ': ' + path)
