"""Native, project-scoped reporting and risk-of-bias assessment workspace."""
from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
import uuid
import webbrowser
from pathlib import Path
from tkinter import filedialog, ttk

from .guideline_audit import GuidelineAuditService, NA_ALLOWED
from . import guideline_library

_TEXT = json.loads((Path(__file__).parent / "locales/guideline_ui.json").read_text(encoding="utf-8"))
_BUNDLED = Path(__file__).parents[1] / "deliverables/gradual_v1_20261103/evaluation.json"


class GuidelineUIMixin:
    def capture_guideline_drafts(self):
        box = getattr(self, "guideline_note", None)
        context = getattr(self, "_guideline_edit_context", None)
        if box is not None and box.winfo_exists() and context:
            self.var("guideline_draft").set(json.dumps({
                "context": context, "note": box.get("1.0", "end-1c"),
                "response": self.guideline_response.current(),
            }, ensure_ascii=False))

    def _gt(self, key):
        pair = _TEXT.get(key)
        return pair[self.lang == "en"] if pair else key

    def _guideline_service(self):
        return GuidelineAuditService(self.project.path)

    def _gbutton(self, parent, key, callback):
        button = ttk.Button(parent, text=self._gt(key), command=lambda: self.safe(callback))
        button.pack(side="left", padx=(0, 6), pady=3)
        return button

    def _gfield(self, parent, key, variable, choose=False):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=self._gt(key), width=27).pack(side="left")
        ttk.Entry(row, textvariable=self.var(variable)).pack(side="left", fill="x", expand=True)
        if choose:
            self._gbutton(row, "choose", lambda: self._guideline_choose(variable))

    def _guideline_choose(self, variable):
        pattern = "*.csv" if variable == "guideline_input" else "*.json"
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("Evidence", pattern)])
        if path:
            self.var(variable).set(path)

    def _guideline_text(self, parent, height=6):
        box = tk.Text(parent, height=height, wrap="word", font=("Malgun Gothic", 10))
        box.pack(fill="both", expand=True, pady=4)
        return box

    @staticmethod
    def _guideline_display(box, value):
        if not box.winfo_exists():
            return
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
        box.configure(state="disabled")

    def build_guidelines(self, parent):
        # The host captured the previous editor before destroying its widgets.
        self._guideline_edit_context = None
        ttk.Label(parent, text=self._gt("scope"), wraplength=1040).pack(fill="x", pady=5)
        self.guideline_notebook = ttk.Notebook(parent)
        self.guideline_notebook.pack(fill="both", expand=True)
        tabs = []
        for name in ("evaluation", "assessment", "remaining", "library"):
            tab = ttk.Frame(self.guideline_notebook, padding=10)
            self.guideline_notebook.add(tab, text=self._gt(name))
            tabs.append(tab)
        self._build_guideline_evaluation(tabs[0])
        self._build_guideline_assessment(tabs[1])
        ttk.Label(tabs[2], text=self._gt("external_only"), wraplength=1000).pack(fill="x", pady=5)
        self.guideline_remaining = self._guideline_text(tabs[2], 22)
        self._build_guideline_library(tabs[3])
        self._refresh_guideline_audits()

    def _build_guideline_evaluation(self, parent):
        ttk.Label(parent, text=self._gt("evaluation_note"), wraplength=1000).pack(fill="x", pady=5)
        self._gfield(parent, "input", "guideline_input", choose=True)
        if not self.var("guideline_replicates").get():
            self.var("guideline_replicates").set("1000")
        self._gfield(parent, "replicates", "guideline_replicates")
        actions = ttk.Frame(parent)
        actions.pack(fill="x")
        self._gbutton(actions, "run", self._run_guideline_evaluation)
        self._gbutton(actions, "cancel", self.cancel_guideline_work)
        ttk.Label(parent, text=self._gt("input_help"), wraplength=1000).pack(fill="x", pady=6)
        self._gfield(parent, "evidence", "guideline_evidence", choose=True)
        actions = ttk.Frame(parent)
        actions.pack(fill="x")
        self._gbutton(actions, "bundled", self._load_bundled_guideline_evaluation)
        self._gbutton(actions, "preview", self._preview_guideline_evaluation)
        self._gbutton(actions, "create", lambda: self._create_guideline_audit(True))
        self.guideline_output = self._guideline_text(parent, 17)
        self._guideline_display(self.guideline_output, self.var("guideline_output_text").get())

    def _build_guideline_assessment(self, parent):
        self._gfield(parent, "title", "guideline_title")
        actions = ttk.Frame(parent)
        actions.pack(fill="x")
        self._gbutton(actions, "empty", lambda: self._create_guideline_audit(False))
        self._gbutton(actions, "refresh", self._refresh_guideline_audits)
        self._gbutton(actions, "check", self._check_guideline_audit)
        self._gbutton(actions, "attach", self._attach_guideline_document)
        self._gbutton(actions, "export", self._export_guideline_audit)
        self._gbutton(actions, "export_pdf", self._export_guideline_pdf)
        self.guideline_picker = ttk.Combobox(parent, state="readonly", width=90)
        self.guideline_picker.pack(fill="x", pady=5)
        self.guideline_picker.bind("<<ComboboxSelected>>", self._select_guideline_audit)
        host = ttk.Frame(parent)
        host.pack(fill="both", expand=True)
        self.guideline_tree = ttk.Treeview(host, columns=("framework", "item", "status", "label"),
                                           show="headings", selectmode="browse", height=11)
        for key, width in (("framework", 155), ("item", 160), ("status", 110), ("label", 430)):
            self.guideline_tree.heading(key, text=self._gt(key))
            self.guideline_tree.column(key, width=width, stretch=key == "label")
        scrollbar = ttk.Scrollbar(host, orient="vertical", command=self.guideline_tree.yview)
        self.guideline_tree.configure(yscrollcommand=scrollbar.set)
        self.guideline_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.guideline_tree.bind("<<TreeviewSelect>>", self._select_guideline_item)
        ttk.Label(parent, text=self._gt("detail")).pack(anchor="w", pady=(6, 0))
        self.guideline_detail = self._guideline_text(parent, 4)
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Label(row, text=self._gt("status"), width=14).pack(side="left")
        self.guideline_response = ttk.Combobox(row, state="readonly", width=27)
        self.guideline_response.pack(side="left")
        ttk.Label(row, text=self._gt("reviewer")).pack(side="left", padx=12)
        ttk.Entry(row, textvariable=self.var("guideline_reviewer")).pack(side="left", fill="x", expand=True)
        ttk.Label(parent, text=self._gt("note")).pack(anchor="w")
        self.guideline_note = self._guideline_text(parent, 4)
        actions = ttk.Frame(parent)
        actions.pack(fill="x")
        self.guideline_save_button = self._gbutton(actions, "save", self._save_guideline_item)

    def _guideline_output(self, value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        self.var("guideline_output_text").set(text)
        box = getattr(self, "guideline_output", None)
        if box is not None and box.winfo_exists():
            self._guideline_display(box, text)
        box = getattr(self, "guideline_library_output", None)
        if box is not None and box.winfo_exists():
            self._guideline_display(box, text)

    def _build_guideline_library(self, parent):
        self.guideline_catalog = guideline_library.catalog()
        ttk.Label(parent, text=self._gt("library_intro"), wraplength=1000).pack(fill="x", pady=4)
        host = ttk.Frame(parent)
        host.pack(fill="both", expand=True)
        self.guideline_library_tree = ttk.Treeview(host, columns=("framework", "label", "version"),
                                                  show="headings", selectmode="browse", height=9)
        for key, width in (("framework", 140), ("label", 420), ("version", 80)):
            self.guideline_library_tree.heading(key, text=self._gt(key))
            self.guideline_library_tree.column(key, width=width, stretch=key == "label")
        scrollbar = ttk.Scrollbar(host, orient="vertical", command=self.guideline_library_tree.yview)
        self.guideline_library_tree.configure(yscrollcommand=scrollbar.set)
        self.guideline_library_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        frameworks = {f["id"]: f for f in self.guideline_catalog["frameworks"]}
        for doc in self.guideline_catalog["documents"]:
            self.guideline_library_tree.insert("", "end", iid=doc["id"], values=(
                frameworks[doc["framework"]]["name"], doc[f"title_{self.lang}"], doc["year"]))
        self.guideline_library_tree.bind("<<TreeviewSelect>>", self._select_guideline_resource)
        actions = ttk.Frame(parent)
        actions.pack(fill="x")
        for key, callback in (("official_page", self._open_guideline_official_page),
                              ("open_original", self._open_guideline_original),
                              ("save_original", self._save_guideline_original),
                              ("download_original", self._download_guideline_original)):
            self._gbutton(actions, key, callback)
        actions = ttk.Frame(parent)
        actions.pack(fill="x")
        self._gbutton(actions, "save_library", self._save_guideline_library)
        self._gbutton(actions, "guide_pdf", self._export_guideline_guide)
        ttk.Label(parent, text=self._gt("print_help"), wraplength=1000).pack(fill="x", pady=3)
        self.guideline_library_detail = self._guideline_text(parent, 10)
        self.guideline_library_output = self._guideline_text(parent, 3)
        selected = self.var("guideline_resource").get()
        if selected not in self.guideline_library_tree.get_children():
            selected = self.guideline_catalog["documents"][0]["id"]
        self.guideline_library_tree.selection_set(selected)
        self._select_guideline_resource()

    def _selected_guideline_resource(self):
        selection = self.guideline_library_tree.selection()
        if not selection:
            raise ValueError(self._gt("select_resource"))
        return next(d for d in self.guideline_catalog["documents"] if d["id"] == selection[0])

    def _select_guideline_resource(self, _event=None):
        if not self.guideline_library_tree.selection():
            return
        doc = self._selected_guideline_resource()
        self.var("guideline_resource").set(doc["id"])
        framework = next(f for f in self.guideline_catalog["frameworks"] if f["id"] == doc["framework"])
        lang = self.lang
        lines = [f"{framework['name']} ({framework['year']}) · {framework[f'role_{lang}']}",
                 framework[f"application_{lang}"], framework[f"scope_{lang}"], "",
                 doc[f"notes_{lang}"], f"{doc['version']} · {self._gt('verified_on')}: {doc['verified_on']}",
                 f"PDF: {doc['url']}", f"{self._gt('source_acquisition')}: {doc.get('acquisition_url', doc['url'])}",
                 f"SHA-256: {doc['sha256']}", doc.get("license", ""), "",
                 self.guideline_catalog[f"external_data_note_{lang}"]]
        if framework.get("article_url"):
            lines.insert(3, framework["article_url"])
        self._guideline_display(self.guideline_library_detail, "\n".join(lines))

    def _guideline_cache_root(self):
        return self.project.path / "research" / "guideline_library"

    def _open_guideline_official_page(self):
        webbrowser.open(self._selected_guideline_resource()["official_url"])

    def _open_guideline_original(self):
        doc_id, cache = self._selected_guideline_resource()["id"], self._guideline_cache_root()

        def complete(result):
            self._guideline_output(result)
            if hasattr(os, "startfile"):
                os.startfile(result["path"])
            else:
                webbrowser.open(Path(result["path"]).as_uri())

        self._guideline_async(lambda _p, _c: guideline_library.obtain(doc_id, cache), complete)

    def _save_guideline_original(self):
        doc, cache = self._selected_guideline_resource(), self._guideline_cache_root()
        destination = filedialog.asksaveasfilename(parent=self.root, title=self._gt("save_original"),
            initialfile=doc["filename"], defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if destination:
            self._guideline_async(lambda _p, _c: guideline_library.copy_original(doc["id"], destination, cache),
                                 self._guideline_output)

    def _download_guideline_original(self):
        doc_id, cache = self._selected_guideline_resource()["id"], self._guideline_cache_root()
        self._guideline_async(lambda _p, _c: guideline_library.obtain(doc_id, cache, prefer_network=True),
                             self._guideline_output)

    def _save_guideline_library(self):
        destination = filedialog.askdirectory(parent=self.root, title=self._gt("save_library"))
        if destination:
            target = Path(destination) / f"SleepWell_official_guidelines_{uuid.uuid4().hex[:8]}"
            cache = self._guideline_cache_root()
            self._guideline_async(lambda _p, _c: guideline_library.save_all(target, cache),
                                 lambda rows: self._guideline_output({"folder": str(target), "documents": len(rows)}))

    def _export_guideline_guide(self):
        destination = filedialog.asksaveasfilename(parent=self.root, title=self._gt("guide_pdf"),
            initialfile="SleepWell_guideline_application_guide.pdf", defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if destination:
            language = self.lang
            self._guideline_async(lambda _p, _c: str(guideline_library.export_guide_pdf(destination, language)),
                                 self._guideline_output)

    def _load_bundled_guideline_evaluation(self):
        if not _BUNDLED.is_file():
            raise ValueError(self._gt("no_bundled"))
        self.var("guideline_evidence").set(str(_BUNDLED))
        self._preview_guideline_evaluation()

    def _preview_guideline_evaluation(self):
        path = Path(self.var("guideline_evidence").get()).expanduser()
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != "sleepwell.gradual_evaluation.v1":
            raise ValueError("unsupported_evaluation_schema")
        selected = [m for m in data.get("metrics", [])
                    if m.get("split", "").lower() == "test" and m.get("policy") == "gradual_v1"
                    and m.get("weighting") == "participant"]
        lines = ["gradual_v1 · " + self._gt("evaluation"), self._gt("evaluation_note"),
                 "", self._gt("metric_intro"), ""]
        for cohort in sorted({m["cohort"] for m in selected}):
            rows = [m for m in selected if m["cohort"] == cohort]
            first = rows[0]
            lines.append(f"{cohort.upper()} · {first['n_participants']}{self._gt('people')} / "
                         f"{first['n_nights']}{self._gt('nights')}")
            for arm in ("data", "hybrid", "physical", "recent_median"):
                values = {m["metric"]: m for m in rows if m["arm"] == arm}
                joint = values.get("joint_mae_min")
                if not joint:
                    continue
                ci = (f" ({joint['ci95_low']:.2f}–{joint['ci95_high']:.2f})"
                      if joint.get("ci95_low") is not None else "")
                lines.append(f"  {arm}: {self._gt('joint')} {joint['value']:.2f}{ci}")
                endpoints = [f"{self._gt(e)} {values[e + '_mae_min']['value']:.2f}"
                             for e in ("onset", "wake") if e + "_mae_min" in values]
                lines.append("    " + " · ".join(endpoints))
            lines.append("")
        lines += [self._gt("limitations")] + ["• " + v for v in data.get("limitations", [])]
        self._guideline_output("\n".join(lines))

    def _run_guideline_evaluation(self):
        from .gradual_evaluation import evaluate
        input_path = Path(self.var("guideline_input").get()).expanduser().resolve()
        if not input_path.is_file():
            raise FileNotFoundError(input_path)
        count = int(self.var("guideline_replicates").get())
        if not 100 <= count <= 10000:
            raise ValueError("bootstrap_replicates must be between 100 and 10000")
        output = self.project.path / "research" / "evaluations" / f"gradual_{uuid.uuid4().hex}"

        def work(progress, cancelled):
            return evaluate(input_path, output, bootstrap_replicates=count, seed=20261103,
                            progress=progress, cancelled=cancelled)

        def complete(_result):
            self.var("guideline_evidence").set(str(output / "evaluation.json"))
            self._preview_guideline_evaluation()

        self._guideline_async(work, complete)

    def _create_guideline_audit(self, with_evidence):
        evidence = self.var("guideline_evidence").get().strip() if with_evidence else None
        if with_evidence and not evidence:
            raise ValueError(self._gt("evidence"))
        audit = self._guideline_service().create(evaluation_path=evidence, language=self.lang,
                    title=self.var("guideline_title").get() or "SleepWell · TRIPOD+AI / PROBAST+AI",
                    template="sleepwell_fixed" if with_evidence else "blank")
        self.var("guideline_selected").set(audit["audit_id"])
        self._refresh_guideline_audits()
        self.guideline_notebook.select(1)

    def _refresh_guideline_audits(self):
        self.guideline_audits = self._guideline_service().list()
        labels = [f"{a['title']} · v{a['version']} · {a['audit_id'][-8:]}" for a in self.guideline_audits]
        self.guideline_picker.configure(values=labels)
        selected = self.var("guideline_selected").get()
        index = next((i for i, a in enumerate(self.guideline_audits) if a["audit_id"] == selected), -1)
        if index < 0 and self.guideline_audits:
            index = 0
        if index >= 0:
            self.guideline_picker.current(index)
            self._select_guideline_audit()

    def _select_guideline_audit(self, _event=None):
        self.capture_guideline_drafts()
        index = self.guideline_picker.current()
        if index < 0:
            return
        self.var("guideline_selected").set(self.guideline_audits[index]["audit_id"])
        audit = self._guideline_service().get(self.var("guideline_selected").get())
        self.guideline_current = audit
        self.var("guideline_title").set(audit["title"])
        self.guideline_tree.delete(*self.guideline_tree.get_children())
        self.guideline_rows = {}
        rows = [("tripod", r["item_id"], r) for r in audit["tripod"]["items"]]
        rows += [("probast", f"{r['phase']}:{r['item_id']}", r) for r in audit["probast"]["questions"]]
        rows += [("domains", r["domain_id"], r) for r in
                 audit["probast"]["domains"] + audit["probast"]["overall_judgments"]]
        for section, item_id, row in rows:
            iid = f"{section}/{item_id}"
            self.guideline_rows[iid] = (section, item_id, row)
            label = row.get("label_en") if self.lang == "en" else row.get("label")
            label = label or row.get("label") or row.get("domain_id")
            status = row.get("status", row.get("answer", row.get("judgment")))
            self.guideline_tree.insert("", "end", iid=iid,
                values=("TRIPOD+AI" if section == "tripod" else "PROBAST+AI", item_id, self._gt(status), label))
        task_lines = []
        for category, tasks in audit["remaining_tasks"].items():
            task_lines.extend([self._gt(category), *["• " + str(task) for task in tasks], ""])
        self._guideline_display(self.guideline_remaining, "\n".join(task_lines))
        self._guideline_display(self.guideline_detail, "")
        self.guideline_note.delete("1.0", "end")
        self.guideline_response.set("")
        self._guideline_edit_context = None
        draft = json.loads(self.var("guideline_draft").get() or "{}")
        context = draft.get("context", [])
        if len(context) == 2 and context[0] == audit["audit_id"] and context[1] in self.guideline_rows:
            self.guideline_tree.selection_set(context[1])
            self.guideline_tree.see(context[1])
            self._select_guideline_item()

    def _select_guideline_item(self, _event=None):
        selected = self.guideline_tree.selection()
        if not selected:
            return
        section, item_id, row = self.guideline_rows[selected[0]]
        self.capture_guideline_drafts()
        self._guideline_edit_context = [self.var("guideline_selected").get(), selected[0]]
        if section == "tripod":
            choices = ["pending", "satisfied", "partially_satisfied", "not_reported", "unknown", "not_applicable"]
            current, note = row["status"], row.get("note", "")
        elif section == "probast":
            choices = ["Y", "PY", "PN", "N", "NI"] + (["NA"] if item_id in NA_ALLOWED else [])
            current, note = row["answer"], row.get("rationale", "")
        else:
            choices = ["Low", "High", "Unclear"]
            current, note = row["judgment"], row.get("rationale", "")
        self.guideline_choices = choices
        derived = section == "domains" and item_id in {
            "overall:development_quality", "overall:evaluation_risk", "overall:applicability_new_korean_diary"}
        self.guideline_response.configure(values=[f"{c} · {self._gt(c)}" for c in choices],
                                          state="disabled" if derived else "readonly")
        self.guideline_save_button.configure(state="disabled" if derived else "normal")
        self.guideline_response.current(choices.index(current))
        self.guideline_note.delete("1.0", "end")
        self.guideline_note.insert("1.0", note)
        draft = json.loads(self.var("guideline_draft").get() or "{}")
        if draft.get("context") == self._guideline_edit_context:
            self.guideline_note.delete("1.0", "end")
            self.guideline_note.insert("1.0", draft.get("note", ""))
            if 0 <= draft.get("response", -1) < len(choices):
                self.guideline_response.current(draft["response"])
        detail = [row.get("label_en" if self.lang == "en" else "label_ko", row.get("label", item_id)),
                  row.get("note", row.get("rationale", "")), row.get("remaining_action", "")]
        if row.get("evidence_ids"):
            detail.append(self._gt("evidence") + ": " + ", ".join(row["evidence_ids"]))
        self._guideline_display(self.guideline_detail, "\n".join(x for x in detail if x))

    def _save_guideline_item(self):
        selected = self.guideline_tree.selection()
        if not selected:
            raise ValueError(self._gt("select_item"))
        section, item_id, _row = self.guideline_rows[selected[0]]
        response_index = self.guideline_response.current()
        if response_index < 0:
            raise ValueError(self._gt("status"))
        response = self.guideline_choices[response_index]
        note = self.guideline_note.get("1.0", "end-1c")
        fields = {"status": response, "note": note} if section == "tripod" else (
            {"answer": response, "rationale": note} if section == "probast" else
            {"judgment": response, "rationale": note})
        audit = self._guideline_service().revise(self.var("guideline_selected").get(),
                    {section: {item_id: fields}, "title":self.var("guideline_title").get()},
                    assessed_by=self.var("guideline_reviewer").get().strip())
        self.var("guideline_selected").set(audit["audit_id"])
        self._guideline_edit_context = None
        self.var("guideline_draft").set("")
        self._refresh_guideline_audits()
        self.guideline_tree.selection_set(selected[0])
        self.guideline_tree.see(selected[0])

    def _selected_guideline_id(self):
        value = self.var("guideline_selected").get()
        if not value:
            raise ValueError(self._gt("no_audit"))
        return value

    def _check_guideline_audit(self):
        result = self._guideline_service().check(self._selected_guideline_id())
        self._guideline_display(self.guideline_detail, result)
        self._guideline_output(result)

    def _attach_guideline_document(self):
        audit_id = self._selected_guideline_id()
        reviewer = self.var("guideline_reviewer").get().strip()
        if not reviewer:
            raise ValueError(self._gt("reviewer"))
        path = filedialog.askopenfilename(parent=self.root, title=self._gt("attach"),
                                         filetypes=[("Evidence", "*.md *.txt *.json *.pdf *.docx *.csv")])
        if not path:
            return
        links = {}
        selected = self.guideline_tree.selection()
        if selected:
            section, item_id, _row = self.guideline_rows[selected[0]]
            if section in {"tripod", "probast"}:
                links[section] = [item_id]
        audit = self._guideline_service().attach_document(audit_id, path, Path(path).name,
                                assessed_by=reviewer, item_links=links)
        self.var("guideline_selected").set(audit["audit_id"])
        self._refresh_guideline_audits()

    def _export_guideline_audit(self):
        service, audit_id = self._guideline_service(), self._selected_guideline_id()
        destination = filedialog.askdirectory(parent=self.root, title=self._gt("export"))
        if destination:
            self._guideline_async(lambda _p, _c: service.export(audit_id, destination), self._guideline_output)

    def _export_guideline_pdf(self):
        service, audit_id = self._guideline_service(), self._selected_guideline_id()
        destination = filedialog.asksaveasfilename(parent=self.root, title=self._gt("export_pdf"),
            initialfile=f"{audit_id}.pdf", defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if destination:
            self._guideline_async(lambda _p, _c: str(service.export_pdf(audit_id, destination)), self._guideline_output)

    def cancel_guideline_work(self):
        event = getattr(self, "_guideline_cancel", None)
        if event and getattr(self, "_guideline_busy", False):
            event.set()
            self.status.set(self._gt("cancel_requested"))

    def _guideline_async(self, work, complete):
        if getattr(self, "_guideline_busy", False):
            raise ValueError(self._gt("running"))
        self._guideline_busy = True
        self._guideline_cancel = threading.Event()
        messages = queue.Queue()
        self.status.set(self._gt("running"))

        def progress(*args, **kwargs):
            messages.put(("progress", " · ".join(str(x) for x in args) or str(kwargs)))

        def worker():
            try:
                result = work(progress, self._guideline_cancel.is_set)
                messages.put(("result", result))
            except Exception as error:
                messages.put(("error", str(error)))

        def poll():
            try:
                while True:
                    kind, value = messages.get_nowait()
                    if kind == "progress":
                        self.status.set(f"{self._gt('running')} · {value}")
                        continue
                    self._guideline_busy = False
                    if kind == "error":
                        self.status.set(f"{self._gt('failed')}: {value}")
                        self._guideline_output({"error": value})
                    else:
                        try:
                            complete(value)
                            self.status.set(self._gt("completed"))
                        except Exception as error:
                            self.status.set(f"{self._gt('failed')}: {error}")
                            self._guideline_output({"error": str(error)})
                    return
            except queue.Empty:
                self.root.after(50, poll)

        threading.Thread(target=worker, daemon=True, name="sleepwell-guidelines").start()
        self.root.after(25, poll)
