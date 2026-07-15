/**
 * NKD Sigma Curve – Vue 3 extension entry point.
 *
 * When built with Vite (`npm run build`), this file is compiled into
 * web/nkd_sigma_curve.js and replaces the hand-written vanilla JS widget
 * with a proper Vue component mounted as a DOM widget.
 *
 * The ComfyUI extension hook (`beforeRegisterNodeDef`) intercepts the
 * NKDSigmaCurve node type and mounts a Vue app inside the DOM widget
 * element for every new node instance.
 */

import { createApp, reactive } from "vue";
import { app as comfyApp } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import SigmaCurveWidget from "@/SigmaCurveWidget.vue";

const NODE_NAME = "NKDSigmasCurve";
const EXT_NAME  = "NKD.SigmasCurve.Vue";

function keepDomWidgetSized(node: any, container: HTMLElement): () => void {
  const MAX_MARGIN = 40;
  let enforcingW = false;
  let goodMargin = 15;
  const vueMode = () => !!(window as any).LiteGraph?.vueNodesMode;
  const clamp = () => {
    if (enforcingW) return;
    if (vueMode()) { if (container.style.width) container.style.width = ""; return; }
    const nodeW = node.size?.[0]; if (!nodeW) return;
    const host = container.parentElement;
    const hostW = host ? host.clientWidth : 0;
    const broken = hostW > 0 && (hostW > nodeW * 1.2 || hostW < nodeW * 0.7);
    if (!broken) {
      if (container.style.width) { enforcingW = true; container.style.width = ""; requestAnimationFrame(() => { enforcingW = false; }); }
      const cw = container.clientWidth;
      if (cw > 0 && cw <= nodeW && cw >= nodeW - MAX_MARGIN) goodMargin = nodeW - cw;
      return;
    }
    const ref = Math.round(nodeW - goodMargin);
    if (ref > 0 && Math.abs(container.clientWidth - ref) > 2) {
      enforcingW = true; container.style.boxSizing = "border-box"; container.style.width = ref + "px";
      requestAnimationFrame(() => { enforcingW = false; });
    }
  };
  clamp();
  const ro = new ResizeObserver(clamp);
  ro.observe(container);
  const origResize = node.onResize;
  node.onResize = function () { origResize?.apply(this, arguments); clamp(); };
  const iv = window.setInterval(clamp, 250);
  return () => { ro.disconnect(); clearInterval(iv); };
}

comfyApp.registerExtension({
  name: EXT_NAME,

  async beforeRegisterNodeDef(
    nodeType: any,
    nodeData: { name: string },
    _app: any
  ): Promise<void> {
    if (nodeData.name !== NODE_NAME) return;

    const origCreated: (() => void) | undefined =
      nodeType.prototype.onNodeCreated;

    nodeType.prototype.onNodeCreated = function (this: any) {
      const result = origCreated?.apply(this, arguments as any);

      // ── Locate widgets ───────────────────────────────────────────────
      const curveDataWidget = this.widgets?.find(
        (w: any) => w.name === "curve_data"
      );
      const stepsWidget    = this.widgets?.find((w: any) => w.name === "steps");
      const maxSigmaWidget = this.widgets?.find((w: any) => w.name === "max_sigma");

      // Reactive wrappers so the Vue computed values re-run when the
      // LiteGraph widget values change (plain objects are not reactive).
      const stepsProxy    = reactive({ value: stepsWidget?.value    ?? 20 });
      const maxSigmaProxy = reactive({ value: maxSigmaWidget?.value ?? 1  });

      // Hide curve_data from the UI entirely. socketless=True in the Python
      // schema is not honored by the V3 frontend: it both renders the widget
      // and creates a phantom input socket that lines up next to "steps",
      // tricking users into connecting STRING wires there. The widget must
      // stay in this.widgets so its value is included in the serialised
      // workflow (the backend needs curve_data on execute), but we set
      // every flag the renderer/layouter respects so nothing is drawn and
      // no vertical space is reserved.
      if (curveDataWidget) {
        curveDataWidget.type           = "hidden";
        curveDataWidget.hidden         = true;
        curveDataWidget.computedHeight = 0;
        curveDataWidget.computeSize    = () => [0, -4];
      }
      const cdIdx = this.inputs?.findIndex((inp: any) => inp.name === "curve_data");
      if (cdIdx !== undefined && cdIdx >= 0) {
        this.removeInput(cdIdx);
      }

      // ── Build container div for the Vue app ──────────────────────────
      const container = document.createElement("div");
      container.style.cssText =
        "width:100%;box-sizing:border-box;overflow:hidden;" +
        "border-top:1px solid rgba(255,255,255,0.06);";

      // Mount the Vue component into the container
      const vueApp = createApp(SigmaCurveWidget, {
        /** Called whenever the curve changes */
        onChange: (json: string) => {
          if (curveDataWidget) curveDataWidget.value = json;
          this.setDirtyCanvas(true);
        },
        stepsWidget:       stepsProxy,
        maxSigmaWidget:    maxSigmaProxy,
        onFetchReference:  () => fetchReference(),
      });

      const instance = vueApp.mount(container) as InstanceType<
        typeof SigmaCurveWidget
      >;

      // ── Reference sigmas: read from the connected node's output ─────
      // When a SIGMAS source is connected to our reference_sigmas input,
      // we listen for `executed` events and extract the tensor values from
      // the upstream node's output so the Vue widget can draw the overlay.
      const self = this;
      function onExecuted(e: Event): void {
        // The Python node sends reference_sigmas back as UI metadata in its
        // own executed event. We only care about events from our own node id.
        const detail = (e as CustomEvent).detail as {
          output?: Record<string, unknown>;
          node?: string | number;
        };
        // In subgraphs the node id is compound e.g. "916:915" — match the tail.
        const nodeId = String(detail?.node ?? "");
        const selfId = String(self.id);
        if (nodeId !== selfId && !nodeId.endsWith(`:${selfId}`)) return;

        const refVals = detail?.output?.["reference_sigmas"];
        if (Array.isArray(refVals) && refVals.length > 0) {
          instance.setReferenceSigmas?.(refVals.flat().map(Number));
        }
      }

      function fetchReference(): void {
        const refInput = self.inputs?.find((inp: any) => inp.name === "reference_sigmas");
        if (!refInput?.link) return;
        const linksMap = comfyApp.graph.links;
        const link = (linksMap instanceof Map)
          ? linksMap.get(refInput.link)
          : linksMap[refInput.link];
        if (!link) return;
        // Queue only our own node as the target — ComfyUI will walk upstream
        // dependencies automatically to satisfy the reference_sigmas input.
        comfyApp.queuePrompt(0, 1, [self.id]);
      }

      api.addEventListener("executed", onExecuted);

      // Clear reference overlay when the link is removed
      const origConnectChange = this.onConnectionsChange;
      this.onConnectionsChange = function (this: any) {
        origConnectChange?.apply(this, arguments as any);
        const refInput = self.inputs?.find((inp: any) => inp.name === "reference_sigmas");
        const connected = !!refInput?.link;
        instance.setReferenceConnected?.(connected);
      };

      // ── Resolve the effective value of an input ──────────────────────
      // When a widget has been converted to an input and a link is wired
      // in, the local widget.value is stale — the real value lives on the
      // upstream node. Walk the link to fetch it so the curve preview
      // updates live as the user edits the upstream widget.
      const resolveInputValue = (widgetName: string, fallback: any): any => {
        const slotIdx = self.inputs?.findIndex((inp: any) => inp.name === widgetName);
        const slot = slotIdx !== undefined && slotIdx >= 0 ? self.inputs[slotIdx] : null;
        if (slot?.link != null) {
          const linksMap: any = comfyApp.graph.links;
          const link = linksMap instanceof Map ? linksMap.get(slot.link) : linksMap[slot.link];
          if (link) {
            const upstream = comfyApp.graph.getNodeById?.(link.origin_id)
                          ?? (comfyApp.graph as any)._nodes_by_id?.[link.origin_id];
            // PrimitiveNode and friends expose their value via widgets[0].
            // Fall back to scanning outputs_values / properties when needed.
            const w = upstream?.widgets?.find((ww: any) =>
              ww?.name === "value" || ww?.name === widgetName
            ) ?? upstream?.widgets?.[0];
            if (w && w.value !== undefined && w.value !== null) return w.value;
          }
        }
        const local = self.widgets?.find((w: any) => w.name === widgetName);
        return local?.value ?? fallback;
      };

      // ── Sync widget values into the reactive proxies ─────────────────
      // Primary path (v1 + v2): chain onto each widget's callback, which
      // LiteGraph calls synchronously when the user changes the value.
      // Vue detects the proxy mutation and re-evaluates extSteps / extMaxSigma.
      if (stepsWidget) {
        const origCb = stepsWidget.callback;
        stepsWidget.callback = function (this: any, value: any) {
          origCb?.call(this, value);
          if (stepsProxy.value !== value) stepsProxy.value = value;
        };
      }
      if (maxSigmaWidget) {
        const origCb = maxSigmaWidget.callback;
        maxSigmaWidget.callback = function (this: any, value: any) {
          origCb?.call(this, value);
          if (maxSigmaProxy.value !== value) maxSigmaProxy.value = value;
        };
      }

      // Fallback (v1 only): onDrawBackground fires every canvas frame.
      // Also used as a retry loop for the v1 blank-widget-on-first-load issue.
      let v1NeedsInit = true;
      const origDrawBg = this.onDrawBackground;
      this.onDrawBackground = function (this: any, ctx: CanvasRenderingContext2D) {
        origDrawBg?.apply(this, arguments as any);
        // Resolve effective values (upstream link wins over local widget) so
        // the preview updates live when steps/max_sigma come from another node.
        const effSteps    = resolveInputValue("steps", 20);
        const effMaxSigma = resolveInputValue("max_sigma", 1.0);
        if (stepsProxy.value    !== effSteps)    stepsProxy.value    = effSteps;
        if (maxSigmaProxy.value !== effMaxSigma) maxSigmaProxy.value = effMaxSigma;
        // Once the canvas has real CSS dimensions, forceResize resolves the
        // blank-widget-on-first-load issue in v1 mode.
        if (v1NeedsInit && instance.forceResize?.()) v1NeedsInit = false;
      };

      // Canvas logical dimensions (must match SigmaCurveWidget.vue constants)
      const CANVAS_W  = 400;
      const CANVAS_H  = 200;
      const CANVAS_AR = CANVAS_H / CANVAS_W; // 0.625

      // barH is measured from the real DOM in requestAnimationFrame below.
      // Start with a safe overestimate so the node is never too short before
      // the measurement fires.
      let barH = 50;
      // Small margin so the node body reaches past the canvas — the canvas renderer
      // reserves the widget row a touch short, which would clip the editor's bottom.
      const ROW_SAFETY = 8;

      // ── Register DOM widget ──────────────────────────────────────────
      // IMPORTANT: Do NOT set domWidget.computeSize. v1's _arrangeWidgets
      // calls widget.computeSize() with ZERO arguments; our old lambda
      // (w) => [w, …] received w=undefined → NaN → collapsed widget.
      // Without computeSize, v1 falls through to computeLayoutSize which
      // reads getMinHeight/getMaxHeight/getHeight from options — correct.
      const domWidget = this.addDOMWidget(
        "sigma_curve_editor",
        "CURVE_EDITOR",
        container,
        {
          getValue: (): string => instance.serialise(),
          setValue: (val: string): void => {
            instance.deserialise(val);
            if (curveDataWidget) curveDataWidget.value = val;
          },
          serialize: false,
          hideOnZoom: false,
          // v1 (DOMWidgetImpl.computeLayoutSize) reads these callbacks to
          // allocate vertical space. All three return the same fixed height
          // so the widget gets exactly the space it needs.
          getMinHeight: () => Math.round((this.size?.[0] || CANVAS_W) * CANVAS_AR) + barH + ROW_SAFETY,
          getMaxHeight: () => Math.round((this.size?.[0] || CANVAS_W) * CANVAS_AR) + barH + ROW_SAFETY,
          getHeight:    () => Math.round((this.size?.[0] || CANVAS_W) * CANVAS_AR) + barH + ROW_SAFETY,
        }
      );

      const _nkdW = keepDomWidgetSized(this, container);

      // Enforce minimum width and lock height proportionally on every resize so
      // the node border never ends up behind the DOM widget.
      const origResize = this.onResize;
      this.onResize = function (this: any, size: [number, number]) {
        origResize?.apply(this, arguments as any);
        if (size[0] < CANVAS_W) size[0] = CANVAS_W;
        size[1] = this.computeSize(size[0])[1];
      };

      // Safety net: ensure node.computeSize() never reports less than the widget needs.
      const origComputeSize = this.computeSize.bind(this);
      this.computeSize = function (_w?: number): [number, number] {
        // Call without arguments: newer LiteGraph computeSize(out?) takes an
        // optional output *array*, not a width number. Passing our width number
        // would trigger "Cannot create property '0' on number" at LGN:1788.
        const sz: [number, number] = origComputeSize();
        const width = sz[0] || this.size[0];
        const needed = Math.round(width * CANVAS_AR) + barH + ROW_SAFETY;
        if (sz[1] < needed) sz[1] = needed;
        return sz;
      };

      // onNodeCreated fires BEFORE LiteGraph restores widget values from the
      // saved workflow JSON, so curveDataWidget.value is still the default here.
      // For brand-new nodes that is correct; for nodes loaded from a workflow
      // we must wait for onConfigure (called after all widget values are set).
      const origConfigure = this.onConfigure;
      this.onConfigure = function (this: any, _data: any) {
        origConfigure?.apply(this, arguments as any);
        const saved = curveDataWidget?.value;
        if (saved) instance.deserialise(saved);
        // In v1 mode the widget element may now have real dimensions after the
        // graph is configured; try to resolve the canvas size immediately.
        if (instance.forceResize?.()) v1NeedsInit = false;
      };

      // Measure the real controls-bar height after the DOM is rendered, update
      // barH (both closures above already reference it), then force the node to
      // adopt the corrected size so the border always wraps the content.
      //
      // Use offsetHeight, NOT getBoundingClientRect().height. The latter is in
      // screen space and therefore includes the LiteGraph canvas zoom transform
      // (the CSS `transform: scale()` ComfyUI applies to DOM widgets), while
      // this.size / computeSize / getHeight all work in unzoomed graph units.
      // Mixing the two scaled barH by the zoom level, which (a) left an abrupt
      // empty margin under the canvas at scale > 1, and (b) made the node jump
      // while corner-resizing because the ceil() of the fractional scaled height
      // flipped ±1px each reflow, re-triggering setSize → ResizeObserver. The
      // border-box offsetHeight is an integer and transform-independent.
      const remeasureBar = () => {
        const barEl = container.querySelector(".nkd-bar") as HTMLElement | null;
        const measured = barEl ? barEl.offsetHeight : 0;
        if (measured > 0 && measured !== barH) {
          barH = measured;
          const sz = this.computeSize(this.size[0]);
          this.setSize(sz);
          this.setDirtyCanvas(true, true);
        }
      };

      requestAnimationFrame(() => {
        remeasureBar();
        if (instance.forceResize?.()) v1NeedsInit = false;

        const sz = this.computeSize(this.size[0]);
        this.setSize(sz);
        this.setDirtyCanvas(true, true);
      });

      // Re-measure whenever the bar grows or shrinks (e.g. reference row appearing)
      const barObserver = new ResizeObserver(remeasureBar);
      requestAnimationFrame(() => {
        const barEl = container.querySelector(".nkd-bar") as HTMLElement | null;
        if (barEl) barObserver.observe(barEl);
      });

      // Clean up the Vue app when the node is removed
      const origRemoved = this.onRemoved;
      this.onRemoved = function (this: any) {
        _nkdW();
        api.removeEventListener("executed", onExecuted);
        barObserver.disconnect();
        instance.cleanup?.();
        vueApp.unmount();
        origRemoved?.apply(this, arguments as any);
      };

      return result;
    };
  },
});
