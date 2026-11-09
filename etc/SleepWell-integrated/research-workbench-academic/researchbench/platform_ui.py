"""Shared native workspace panels over the preserved experiment engines."""
from __future__ import annotations
import json
import tempfile
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import NavigationToolbar2Tk
from .app import tree
from .canvas import ResearchCanvas
from .data_contract import default_mapping, validate_mapping, SLEEP_ROLES
from .result_adapter import load_runs, compare, model_color
from .academic_store import write_json
from .academic_plotting import model_label

LABELS=json.loads((Path(__file__).parent/'locales/platform.json').read_text(encoding='utf-8'))

class PlatformMixin:
    def pt(self,key): return LABELS.get(key,[key,key])[self.lang=='en']

    def pbutton(self,parent,key,callback):
        button=ttk.Button(parent,text=self.pt(key),command=lambda:self.safe(callback))
        button.pack(side='left',padx=3,pady=3); return button

    def platform_runs(self):
        workspace=self.var('workspace').get()
        linked=self.var('linked_general').get()
        if workspace=='tabular': return load_runs(linked) if linked else []
        views=load_runs(self.project.path)
        if workspace=='knowledge' and linked:
            views.extend(load_runs(linked))
            if len({v['run_id'] for v in views}) != len(views):
                raise ValueError('duplicate_run_ids_across_linked_projects')
        return views

    def link_general(self):
        path=filedialog.askdirectory(parent=self.root,title=self.pt('link_general'))
        if path:
            runs=load_runs(path)
            state=json.loads((Path(path)/'project.json').read_text(encoding='utf-8'))
            if state.get('format_version')!=1: raise ValueError('general_project_required')
            self.var('linked_general').set(path); self.save(); self.navigate(self.page)

    def workspace_changed(self):
        self.capture()
        if self.var('workspace').get()!='sleepwell' and self.page=='prospective': self.page='overview'
        self.save(); self.build()

    def build_workspace_intro(self,parent):
        ttk.Label(parent,text=self.pt('workspace_help'),wraplength=950).pack(fill='x',pady=8)
        row=ttk.Frame(parent); row.pack(fill='x')
        self.pbutton(row,'link_general',self.link_general)
        self.button(row,'general',self.general_window)
        ttk.Label(parent,text=self.var('linked_general').get(),wraplength=950).pack(fill='x')

    def platform_readiness(self,parent):
        from .evidence_library import EvidenceLibrary
        from .reporting import ReportService
        runs=self.platform_runs()
        with EvidenceLibrary(self.project.path) as library:
            sources=library.list_sources(); reports=ReportService(self.project.path,library).list_reports()
        values=f"{self.pt('report_ready')} · {self.pt('result_count')}: {len(runs)} · {self.pt('source_count')}: {len(sources)} · {self.pt('reports')}: {len(reports)}"
        ttk.Label(parent,text=values,wraplength=1000).pack(fill='x',pady=8)
        ttk.Label(parent,text=self.pt('review_required'),style='AcademicNote.TLabel').pack(fill='x')
        row=ttk.Frame(parent); row.pack(fill='x')
        self.pbutton(row,'compare_runs',lambda:self.navigate('results'))
        self.pbutton(row,'document_library',lambda:self.navigate('evidence'))
        self.pbutton(row,'reports',lambda:self.navigate('reports'))

    def data_frame(self):
        if self.var('workspace').get()=='tabular':
            path=self.var('linked_general').get()
            if not path:return None
            root=Path(path)
            frame=pd.read_json(root/'data.json',orient='table',precise_float=True)
            state=json.loads((root/'project.json').read_text(encoding='utf-8'))
            splits=state.get('splits') or {}
            if splits:
                split_id=next(iter(splits)); rows=splits[split_id]['train_rows']
                self.var('data_scope').set(f"{self.pt('development_only')} · split={split_id} · n={len(rows)}/{len(frame)}")
                return frame.iloc[rows].copy()
            self.var('data_scope').set(self.pt('before_split'))
            return frame
        item=self.project.state.get('input')
        self.var('data_scope').set(self.pt('sleepwell_scope'))
        return pd.read_csv(self.project.snapshot_path(item['snapshot'])) if item else None

    def build_data(self,parent):
        if self.var('workspace').get()=='tabular': self.build_workspace_intro(parent)
        else:
            row=ttk.Frame(parent); row.pack(fill='x'); self.button(row,'import',self.import_data)
        frame=self.data_frame()
        if frame is None:
            ttk.Label(parent,text=self.t('input_missing')).pack(); return
        self.mapping_frame=frame
        ttk.Label(parent,text=self.var('data_scope').get(),wraplength=1000,style='AcademicNote.TLabel').pack(fill='x',pady=5)
        mapping_path=self.project.path/'research'/'column_mapping.json'
        from .data import fingerprint
        stored=json.loads(mapping_path.read_text(encoding='utf-8')) if mapping_path.exists() else {}
        mapping=stored.get('columns',{}) if stored.get('data_hash')==fingerprint(frame) else {}
        self.column_mapping=default_mapping(frame)
        self.column_mapping.update({k:v for k,v in mapping.items() if k in frame})
        groups=frame['participant_id'].nunique() if 'participant_id' in frame else self.pt('unknown')
        ttk.Label(parent,text=f"{self.pt('rows')}: {len(frame):,} · {self.pt('participants')}: {groups} · {self.pt('missing')}: {int(frame.isna().sum().sum()):,} · {self.pt('duplicates')}: {int(frame.duplicated().sum()):,}",wraplength=1000).pack(fill='x',pady=8)
        ttk.Label(parent,text=self.pt('mapping_note'),wraplength=1000).pack(fill='x',pady=5)
        self.mapping_tree=tree(parent,['column','type','missing','unique','role'],[self.pt(k) for k in ['original_column','type','missing','unique','role']],height=7)
        for col in frame:
            self.mapping_tree.insert('','end',iid=str(col),values=(col,str(frame[col].dtype),f'{frame[col].isna().sum()} ({frame[col].isna().mean():.1%})',frame[col].nunique(),self.column_mapping[str(col)]['role']))
        edit=ttk.Frame(parent); edit.pack(fill='x')
        for i,key in enumerate(['role','type','unit','timezone','allowed','missing_rule']):
            cell=ttk.Frame(edit); cell.grid(row=i//3,column=i%3,sticky='ew',padx=6,pady=4)
            ttk.Label(cell,text=self.pt(key)).pack(anchor='w')
            options={'role':list(SLEEP_ROLES)+['feature','target','id','group','time','exclude'],'type':['original','numeric','datetime'],'missing_rule':['retain','reject']}.get(key)
            widget=ttk.Combobox(cell,values=options,textvariable=self.var('map_'+key),state='readonly') if options else ttk.Entry(cell,textvariable=self.var('map_'+key))
            widget.pack(fill='x')
        for i in range(3): edit.columnconfigure(i,weight=1)
        def choose(event=None):
            selection=self.mapping_tree.selection()
            if selection:
                for key,value in self.column_mapping[selection[0]].items(): self.var('map_'+key).set(value)
        self.mapping_tree.bind('<<TreeviewSelect>>',choose)
        row=ttk.Frame(parent); row.pack(fill='x')
        self.pbutton(row,'save_mapping',self.save_mapping)
        if self.var('workspace').get()!='tabular': self.pbutton(row,'apply_mapping',self.apply_mapping)
        ttk.Label(parent,text=self.pt('scope_limit'),wraplength=1000).pack(fill='x',pady=5)
        ttk.Label(parent,text=f"{self.t('preview_note')} · n={min(100,len(frame))}/{len(frame)}").pack(fill='x')
        preview=tree(parent,list(frame.columns),list(frame.columns),height=5)
        for row in frame.head(100).itertuples(index=False,name=None): preview.insert('','end',values=row)

    def save_mapping(self):
        selected=self.mapping_tree.selection()
        if selected:
            col=selected[0]
            self.column_mapping[col]={k:self.var('map_'+k).get() for k in ['role','type','unit','timezone','allowed','missing_rule']}
        _,rules=validate_mapping(self.mapping_frame,self.column_mapping)
        from .data import fingerprint
        source_project=self.var('linked_general').get() if self.var('workspace').get()=='tabular' else str(self.project.path)
        write_json(self.project.path/'research'/'column_mapping.json',{'schema':1,'source_project':source_project,'data_hash':fingerprint(self.mapping_frame),'columns':rules})
        if selected: self.mapping_tree.set(selected[0],'role',rules[selected[0]]['role'])
        self.status.set(self.t('saved'))

    def apply_mapping(self):
        self.save_mapping()
        mapped,rules=validate_mapping(self.mapping_frame,self.column_mapping,sleepwell=True)
        original=dict(self.project.state.get('input') or {})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'mapped_input.csv'; mapped.to_csv(path,index=False,encoding='utf-8-sig')
            self.project.import_data(path,synthetic=bool(original.get('synthetic')))
        new=self.project.state['input']
        write_json(self.project.path/'research'/'mappings'/(new['sha256']+'.json'),{'schema':1,'source':original,'derived':new,'columns':rules})
        self.refresh_header(); self.navigate('data')

    def build_comparison(self,parent):
        if self.var('workspace').get()=='tabular': self.build_workspace_intro(parent)
        ttk.Label(parent,text=self.pt('select_runs'),wraplength=1000).pack(fill='x',pady=6)
        self.comparison_views=self.platform_runs()
        if not self.comparison_views:
            ttk.Label(parent,text=self.pt('no_runs')).pack(fill='x'); return
        views=self.comparison_views
        controls=ttk.Frame(parent); controls.pack(fill='x')
        self.compare_list=tk.Listbox(controls,selectmode='extended',exportselection=False,height=5,width=55)
        self.compare_list.pack(side='left',fill='both',expand=True)
        saved=json.loads(self.var('comparison_selection').get() or '[]')
        for i,view in enumerate(views):
            self.compare_list.insert('end',f"{view['name']} · {view['run_id'][:8]} · {view['evaluation']['evaluation_type']}")
            if view['run_id'] in saved or not saved and i>=max(0,len(views)-2): self.compare_list.selection_set(i)
        choices=ttk.Frame(controls); choices.pack(side='left',fill='x',padx=8)
        ttk.Label(choices,text=self.pt('baseline_run')).pack(anchor='w')
        self.baseline_combo=ttk.Combobox(choices,state='readonly',values=[f"{v['name']} · {v['run_id'][:8]}" for v in views],width=42)
        baseline=self.var('baseline_run').get()
        self.baseline_combo.current(next((i for i,v in enumerate(views) if v['run_id']==baseline),0)); self.baseline_combo.pack(fill='x')
        metric_keys=sorted({m['metric'] for v in views for m in v['metrics']})
        self.compare_metric=ttk.Combobox(choices,state='readonly',values=metric_keys,textvariable=self.var('comparison_metric'))
        current_metric=self.var('comparison_metric').get()
        study_comparison=any(v['evaluation']['evaluation_type'] in {
            'controlled_comparison', 'pooled_retraining', 'cross_cohort_evaluation',
            'participant_revalidation', 'personalization_comparison',
        } for v in views)
        if study_comparison and 'joint_mae_min' in metric_keys and current_metric in {'','interval_mae_min'}:
            self.var('comparison_metric').set('joint_mae_min')
        elif current_metric not in metric_keys:
            self.var('comparison_metric').set('joint_mae_min' if 'joint_mae_min' in metric_keys else (metric_keys[0] if metric_keys else ''))
        self.compare_metric.pack(fill='x',pady=5)
        self.pbutton(choices,'refresh_runs',self.refresh_comparison)
        self.comparison_sort=self.var('comparison_sort')
        if not self.comparison_sort.get(): self.comparison_sort.set('ascending')
        sort=ttk.Combobox(choices,state='readonly',values=['ascending','descending'],textvariable=self.comparison_sort,width=20); sort.pack(fill='x')
        self.var('comparison_filter')
        ttk.Entry(choices,textvariable=self.var('comparison_filter')).pack(fill='x',pady=4)
        panel=ttk.Panedwindow(parent,orient='vertical'); panel.pack(fill='both',expand=True)
        upper=ttk.Frame(panel); lower=ttk.Frame(panel); panel.add(upper,weight=1); panel.add(lower,weight=2)
        uncertainty_label = '불확실성 (CI 또는 SD)' if self.lang != 'en' else 'Uncertainty (CI or SD)'
        self.comparison_tree=tree(upper,['run','model','value','delta','n','variation','status'],[self.t('name'),self.t('model'),self.pt('value'),self.pt('delta'),self.pt('denominator'),uncertainty_label,self.pt('compatibility')],height=7)
        self.comparison_tree.bind('<<TreeviewSelect>>',self.show_comparison_details)
        self.comparison_plot_host=lower
        actions=ttk.Frame(parent); actions.pack(fill='x')
        self.pbutton(actions,'fit_graph',self.refresh_comparison); self.pbutton(actions,'detach_graph',self.detach_comparison)
        self.button(actions,'export_png',self.export_comparison_figure)
        self.button(actions,'export_table',self.export_comparison_table)
        self.comparison_details=tk.Text(parent,height=6,wrap='word'); self.comparison_details.pack(fill='x')
        for widget in [self.baseline_combo,self.compare_metric,sort]: widget.bind('<<ComboboxSelected>>',lambda e:self.safe(self.refresh_comparison))
        self.compare_list.bind('<<ListboxSelect>>',lambda e:self.safe(self.refresh_comparison))
        self.refresh_comparison()

    def refresh_comparison(self):
        views=self.comparison_views
        baseline=views[self.baseline_combo.current()]
        self.var('baseline_run').set(baseline['run_id'])
        selected=[views[i] for i in self.compare_list.curselection()]
        self.var('comparison_selection').set(json.dumps([v['run_id'] for v in selected]))
        metric=self.var('comparison_metric').get(); self.comparison_rows=[]
        for view in selected:
            for m in view['metrics']:
                if m['metric']!=metric: continue
                if self.var('comparison_filter').get().lower() not in (view['name']+' '+m['model_id']).lower(): continue
                c=compare(baseline,view,metric,m['model_id'] if view['engine']=='sleepwell' else None)
                self.comparison_rows.append({'view':view,'metric':m,'comparison':c})
        # Sort only inside compatibility groups; no global leaderboard across populations.
        descending=self.comparison_sort.get()=='descending'
        cohort_order={'cmi':0,'mesa':1,'macro':2}
        cohort_key=lambda model_id:cohort_order.get(str(model_id).split('.',1)[0].lower(),3)
        color_key=lambda model_id:(str(model_id).split('.',1)[1]
                                   if str(model_id).split('.',1)[0].lower() in cohort_order and '.' in str(model_id)
                                   else str(model_id))
        self.comparison_rows.sort(key=lambda r:(r['comparison']['status'],cohort_key(r['metric']['model_id']),r['view']['evaluation']['scope_hash'] or r['view']['run_id'], (-1 if descending else 1)*(r['metric']['value'] if r['metric']['value'] is not None else float('inf')),r['metric']['model_id']))
        self.comparison_tree.delete(*self.comparison_tree.get_children())
        fmt=lambda v:'—' if v is None else f'{v:.4f}'
        for i,row in enumerate(self.comparison_rows):
            v,m,c=row['view'],row['metric'],row['comparison']; variation=m.get('variation') or {}
            delta=(f"{c['percentage_points']:+.3f} pp" if c.get('percentage_points') is not None else fmt(c['delta']))
            variation_text=(f"95% CI=[{fmt(variation.get('ci95_low'))}, {fmt(variation.get('ci95_high'))}]"
                            if variation.get('is_confidence_interval') else
                            f"SD={fmt(variation.get('sd'))}; k={variation.get('n','—')}")
            self.comparison_tree.tag_configure(m['model_id'],foreground=model_color(color_key(m['model_id'])))
            self.comparison_tree.insert('','end',iid=str(i),tags=(m['model_id'],),values=(v['name'],model_label(m['model_id'],self.lang),fmt(m['value']),delta,f"n={m['n_rows']}; N={m['n_participants'] if m['n_participants'] is not None else '—'}",variation_text,self.pt(c['status'])))
        for child in self.comparison_plot_host.winfo_children(): child.destroy()
        figure=Figure(figsize=(10,5.2),dpi=100,constrained_layout=True); ax=figure.subplots()
        graph_rows=[r for r in self.comparison_rows if r['comparison']['status'] in {'comparable','reference_only'} and r['metric']['value'] is not None]
        if graph_rows:
            for i,row in enumerate(graph_rows):
                m=row['metric']; variation=m.get('variation') or {}; xerr=variation.get('sd')
                if variation.get('is_confidence_interval'):
                    xerr=[[m['value']-variation['ci95_low']],[variation['ci95_high']-m['value']]]
                ax.errorbar(m['value'],i,xerr=xerr,fmt='o',capsize=3,color=model_color(color_key(m['model_id'])))
            ax.set_yticks(range(len(graph_rows)),[f"{r['view']['name']} · {model_label(r['metric']['model_id'],self.lang)}" for r in graph_rows],fontsize=8)
            ax.invert_yaxis(); ax.set_xlabel(f"{metric} · {graph_rows[0]['metric']['unit']} · {self.pt(graph_rows[0]['metric']['direction'])}")
            ci_note=('95% CI · fitted model conditional · excludes training/selection uncertainty'
                     if any((r['metric'].get('variation') or {}).get('is_confidence_interval') for r in graph_rows)
                     else self.pt('variation'))
            status_note=(self.pt('reference_only')
                         if any(r['comparison']['status']=='reference_only' for r in graph_rows)
                         else self.pt('comparable'))
            ax.set_title(f"{status_note} · {ci_note}"); ax.grid(axis='x',alpha=.2)
        else:
            ax.text(.5,.5,self.pt('reference_only'),ha='center',va='center',transform=ax.transAxes); ax.set_axis_off()
        self.comparison_figure=figure
        self.comparison_canvas=ResearchCanvas(figure,master=self.comparison_plot_host)
        self.comparison_canvas.draw(); self.comparison_canvas.get_tk_widget().pack(fill='both',expand=True)
        NavigationToolbar2Tk(self.comparison_canvas,self.comparison_plot_host,pack_toolbar=True)

    def show_comparison_details(self,event=None):
        selected=self.comparison_tree.selection()
        if not selected:return
        row=self.comparison_rows[int(selected[0])]; c=row['comparison']; view=row['view']
        details=f"{self.pt(c['status'])}: {', '.join(c['reasons']) or '✓'}\n{self.pt('comparison_detail')}\n"
        details+='\n'.join(f"{k}: {v['baseline']} → {v['candidate']}" for k,v in c['config_diff'].items())
        details+=f"\n{view['evaluation']}\n{view['warnings']}\n{row['metric'].get('reason') or ''}"
        self.comparison_details.delete('1.0','end'); self.comparison_details.insert('1.0',details)

    def detach_comparison(self):
        window=tk.Toplevel(self.root); window.title(self.pt('compare_runs')); window.geometry('1200x800')
        # A copied figure prevents two canvases sharing callbacks and transforms.
        import copy
        figure=copy.deepcopy(self.comparison_figure); canvas=ResearchCanvas(figure,master=window)
        canvas.draw(); canvas.get_tk_widget().pack(fill='both',expand=True); NavigationToolbar2Tk(canvas,window)

    def export_comparison_figure(self):
        path=filedialog.asksaveasfilename(parent=self.root,defaultextension='.png',filetypes=[('PNG','*.png'),('PDF','*.pdf'),('SVG','*.svg')])
        if path:self.comparison_figure.savefig(path,dpi=int(self.var('dpi').get()),bbox_inches='tight')

    def export_comparison_table(self):
        path=filedialog.asksaveasfilename(parent=self.root,defaultextension='.xlsx',filetypes=[('Excel','*.xlsx'),('CSV','*.csv')])
        if not path:return
        rows=[]
        for row in self.comparison_rows:
            m,c=row['metric'],row['comparison']; variation=m.get('variation') or {}
            rows.append({self.t('name'):row['view']['name'],'run_id':row['view']['run_id'],self.t('model'):m['model_id'],self.pt('value'):m['value'],self.pt('delta'):c['delta'],'unit':m['unit'],'n_rows':m['n_rows'],'n_participants':m['n_participants'],self.pt('compatibility'):self.pt(c['status']),'reasons':','.join(c['reasons']),'fold_SD_not_CI':variation.get('sd'),'ci95_low':variation.get('ci95_low'),'ci95_high':variation.get('ci95_high'),'ci_scope':('conditional_on_fitted_model_excludes_training_selection_uncertainty' if variation.get('is_confidence_interval') else None),'evaluation_calculation':row['view']['evaluation'].get('calculation'),'population':row['view']['evaluation'].get('population')})
        frame=pd.DataFrame(rows)
        if Path(path).suffix=='.xlsx':frame.to_excel(path,index=False)
        else:frame.to_csv(path,index=False,encoding='utf-8-sig')
