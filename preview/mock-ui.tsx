// Dev-only stand-in for @decky/ui used by the page preview harness (never packaged).
//
// Plain DOM approximations of Steam's game-mode (gamepad UI) components, tuned to match
// the real ones' look and, above all, their SIZING on a Steam Deck (1280x800): Field rows
// ~48-56px with 16px side padding and a bottom separator, DialogButton a gray block button
// (width 100%, min-width 100px, 0 16px padding, ~40px tall), Dropdown a button with a caret,
// toggles/sliders on Steam's blue, PanelSection with an uppercase title, Tabs with a tab
// strip on top. Fidelity is "close", not pixel-exact: the real components come from Steam's
// webpack bundle at runtime and cannot be loaded here.
//
// Gamepad navigation is simulated by preview/nav.ts: every focusable leaf gets
// data-nav-leaf, every Focusable gets data-nav-group with its flow-children axis.
import {
  Children,
  CSSProperties,
  ChangeEvent,
  Component,
  FC,
  ReactElement,
  ReactNode,
  cloneElement,
  createContext,
  forwardRef,
  isValidElement,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { createRoot, Root } from "react-dom/client";

// ---- theme ------------------------------------------------------------------------------------

export const STEAM = {
  bg: "#0e141b",
  panel: "#23262e",
  panel2: "#2a2e37",
  button: "#3d4450",
  buttonHover: "#4a5260",
  text: "#ffffff",
  label: "#dcdedf",
  desc: "#8b929a",
  sep: "rgba(255,255,255,0.10)",
  blue: "#1a9fff",
  focus: "#ffffff",
  font: '"Motiva Sans", "Liberation Sans", Arial, Helvetica, sans-serif',
};

// QAM context: PanelSection supplies the side padding there, so Fields drop theirs.
const PanelCtx = createContext(false);

function navAttrs(leaf: boolean, disabled?: boolean): Record<string, string | number | undefined> {
  return leaf && !disabled ? { "data-nav-leaf": "1", tabIndex: 0 } : {};
}

function activateKeys(fn?: (e: any) => void) {
  return fn
    ? (e: React.KeyboardEvent) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          fn(e);
        }
      }
    : undefined;
}

// ---- Focusable --------------------------------------------------------------------------------

export interface FocusableProps extends React.HTMLAttributes<HTMLDivElement> {
  children?: ReactNode;
  "flow-children"?: string;
  focusClassName?: string;
  focusWithinClassName?: string;
  noFocusRing?: boolean;
  onActivate?: (e: any) => void;
  onCancel?: (e: any) => void;
  onSecondaryActionDescription?: ReactNode;
  onSecondaryButton?: (e: any) => void;
  onOptionsActionDescription?: ReactNode;
  onOptionsButton?: (e: any) => void;
  onOKActionDescription?: ReactNode;
  onCancelActionDescription?: ReactNode;
  onMenuActionDescription?: ReactNode;
  onMenuButton?: (e: any) => void;
  actionDescriptionMap?: unknown;
  focusable?: boolean;
  [k: string]: any;
}

const FOCUSABLE_PROPS = new Set([
  "flow-children", "focusClassName", "focusWithinClassName", "noFocusRing", "onActivate", "onCancel",
  "onSecondaryActionDescription", "onSecondaryButton", "onOptionsActionDescription", "onOptionsButton",
  "onOKActionDescription", "onCancelActionDescription", "onMenuActionDescription", "onMenuButton",
  "actionDescriptionMap", "focusable", "onGamepadDirection", "onGamepadFocus", "onGamepadBlur",
  "autoFocus", "navEntryPreferPosition", "navRef", "retainFocus", "childFocusDisabled", "preferredFocus",
]);

function domProps(p: Record<string, any>): Record<string, any> {
  const out: Record<string, any> = {};
  for (const k of Object.keys(p)) if (!FOCUSABLE_PROPS.has(k) && k !== "children") out[k] = p[k];
  return out;
}

export const Focusable = forwardRef<HTMLDivElement, FocusableProps>(function Focusable(props, ref) {
  const { children, onActivate, focusClassName, focusWithinClassName, noFocusRing, className } = props;
  const flow = props["flow-children"];
  const [focused, setFocused] = useState(false);
  const [within, setWithin] = useState(false);
  // A Focusable is a focus target itself when it can be activated (or says so); otherwise
  // it is only a navigation group for its children. nav.ts demotes a "leaf" that turns out
  // to contain other leaves to a group.
  const leaf = !!onActivate || props.focusable === true;
  const cls = [className, focused && focusClassName, within && focusWithinClassName, leaf && !noFocusRing && "pv-ring"]
    .filter(Boolean)
    .join(" ");
  return (
    <div
      ref={ref}
      {...domProps(props)}
      className={cls || undefined}
      data-nav-group={flow ?? "default"}
      {...navAttrs(leaf)}
      onClick={(e) => {
        props.onClick?.(e);
        // A click on a nested control (button, toggle...) belongs to that control.
        const inner = (e.target as HTMLElement).closest("[data-nav-leaf]");
        if (onActivate && (!inner || inner === e.currentTarget)) onActivate(e);
      }}
      onKeyDown={(e) => {
        props.onKeyDown?.(e);
        if (onActivate && e.target === e.currentTarget) activateKeys(onActivate)?.(e);
      }}
      onFocus={(e) => {
        props.onFocus?.(e);
        if (e.target === e.currentTarget) setFocused(true);
        setWithin(true);
      }}
      onBlur={(e) => {
        props.onBlur?.(e);
        if (e.target === e.currentTarget) setFocused(false);
        if (!e.currentTarget.contains(e.relatedTarget as Node)) setWithin(false);
      }}
    >
      {children}
    </div>
  );
});

export const FocusRing: FC<{ children?: ReactNode; className?: string }> = ({ children, className }) => <div className={className}>{children}</div>;
export const GamepadButton = { OK: 1, CANCEL: 2, SECONDARY: 3, OPTIONS: 4, BUMPER_LEFT: 5, BUMPER_RIGHT: 6, DIR_UP: 9, DIR_DOWN: 10, DIR_LEFT: 11, DIR_RIGHT: 12 };

// ---- buttons ----------------------------------------------------------------------------------

export interface DialogButtonProps extends React.HTMLAttributes<HTMLDivElement> {
  disabled?: boolean;
  noFocusRing?: boolean;
  focusable?: boolean;
  onOKButton?: (e: any) => void;
  [k: string]: any;
}

function defined(style?: CSSProperties): CSSProperties {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(style ?? {})) if (v !== undefined && v !== null) out[k] = v;
  return out as CSSProperties;
}

// Steam's DialogButton is a full-width block (pages set width / minWidth to size it); the
// preview can be switched to content width with ?dialogButtonWidth=auto to compare.
const BUTTON_WIDTH = new URLSearchParams(location.search).get("dialogButtonWidth") === "auto" ? "auto" : "100%";

const buttonBase: CSSProperties = {
  display: "block",
  boxSizing: "border-box",
  width: BUTTON_WIDTH,
  minWidth: "100px",
  minHeight: "40px",
  padding: "0 16px",
  lineHeight: "40px",
  background: STEAM.button,
  color: STEAM.text,
  border: "none",
  borderRadius: "2px",
  fontFamily: STEAM.font,
  fontSize: "16px",
  fontWeight: 500,
  textAlign: "center",
  whiteSpace: "nowrap",
  overflow: "hidden",
  textOverflow: "ellipsis",
  cursor: "pointer",
  userSelect: "none",
};

export const DialogButton = forwardRef<HTMLDivElement, DialogButtonProps>(function DialogButton(props, ref) {
  const { children, disabled, style, onClick, className, noFocusRing } = props;
  const rest = domProps(props);
  delete rest.style;
  delete rest.onClick;
  delete rest.disabled;
  delete rest.onOKButton;
  delete rest.className;
  // Steam keeps the 40px line box unless padding is overridden; a padding override in
  // style shrinks the box like the real DialogButton does (its height comes from padding).
  const padded = style && (style.padding !== undefined || style.paddingTop !== undefined);
  // Steam styles DialogButton through a CSS class, so `background: undefined` in the style
  // prop leaves the class value alone: merge only defined keys.
  const s: CSSProperties = { ...buttonBase, ...(padded ? { lineHeight: "20px", minHeight: 0 } : {}), ...defined(style) };
  if (disabled) Object.assign(s, { opacity: 0.45, cursor: "default" });
  return (
    <div
      ref={ref}
      role="button"
      {...rest}
      className={["pv-btn", !noFocusRing && "pv-ring", className].filter(Boolean).join(" ")}
      style={s}
      aria-disabled={disabled || undefined}
      {...navAttrs(true, disabled)}
      onClick={(e) => !disabled && onClick?.(e)}
      onKeyDown={activateKeys((e: any) => !disabled && onClick?.(e))}
    >
      {children}
    </div>
  );
});

export const Button = DialogButton;
export const DialogButtonPrimary = forwardRef<HTMLDivElement, DialogButtonProps>(function DialogButtonPrimary(p, ref) {
  return <DialogButton ref={ref} {...p} style={{ background: STEAM.blue, ...p.style }} />;
});
export const DialogButtonSecondary = DialogButton;

// ---- Field ------------------------------------------------------------------------------------

export interface FieldProps {
  children?: ReactNode;
  label?: ReactNode;
  bottomSeparator?: "standard" | "thick" | "none";
  description?: ReactNode;
  disabled?: boolean;
  icon?: ReactNode;
  inlineWrap?: "keep-inline" | "shift-children-below";
  childrenLayout?: "below" | "inline";
  childrenContainerWidth?: "min" | "max" | "fixed";
  spacingBetweenLabelAndChild?: "none";
  padding?: "none" | "standard" | "compact";
  className?: string;
  highlightOnFocus?: boolean;
  indentLevel?: number;
  verticalAlignment?: "center" | "none";
  focusable?: boolean;
  onActivate?: (e: any) => void;
  onClick?: (e: any) => void;
  style?: CSSProperties;
  [k: string]: any;
}

function hasNavChild(children: ReactNode): boolean {
  // Cheap static guess (nav.ts does the real check on the DOM): any element child counts.
  let found = false;
  Children.forEach(children, (c) => {
    if (isValidElement(c)) found = true;
  });
  return found;
}

export const Field = forwardRef<HTMLDivElement, FieldProps>(function Field(props, ref) {
  const inPanel = useContext(PanelCtx);
  const {
    label, description, children, bottomSeparator = "standard", padding = "standard", childrenLayout,
    icon, indentLevel, disabled, onActivate, onClick, focusable, className, style, childrenContainerWidth,
    inlineWrap,
  } = props;
  const act = onActivate ?? onClick;
  const leaf = !disabled && (!!act || (focusable === true && !hasNavChild(children)) || (focusable === true && !children));
  const side = inPanel ? 0 : 16;
  const pad =
    padding === "none" ? "0" : padding === "compact" ? `6px ${side}px` : `10px ${side}px`;
  const below = childrenLayout === "below" || inlineWrap === "shift-children-below";
  const sep =
    bottomSeparator === "none" ? "none" : bottomSeparator === "thick" ? `3px solid ${STEAM.sep}` : `1px solid ${STEAM.sep}`;
  const hasLabel = label !== undefined && label !== null && label !== "";
  const childBox: CSSProperties = below
    ? { marginTop: hasLabel ? "8px" : 0, width: "100%" }
    : {
        marginLeft: hasLabel ? "16px" : 0,
        flex: childrenContainerWidth === "max" ? "1 1 auto" : "0 1 auto",
        minWidth: 0,
        display: "flex",
        justifyContent: "flex-end",
        alignItems: "center",
        maxWidth: childrenContainerWidth === "fixed" ? "50%" : undefined,
      };
  return (
    <div
      ref={ref}
      className={["pv-field", leaf && "pv-ring", className].filter(Boolean).join(" ")}
      data-nav-group="default"
      {...navAttrs(leaf)}
      onClick={(e) => act && !disabled && act(e)}
      onKeyDown={(e) => e.target === e.currentTarget && activateKeys(act)?.(e)}
      style={{
        boxSizing: "border-box",
        minHeight: padding === "none" ? undefined : padding === "compact" ? "40px" : "48px",
        padding: pad,
        paddingLeft: indentLevel ? `${side + indentLevel * 16}px` : undefined,
        borderBottom: sep,
        display: "flex",
        flexDirection: below ? "column" : "row",
        alignItems: below ? "stretch" : "center",
        justifyContent: "space-between",
        opacity: disabled ? 0.5 : 1,
        fontFamily: STEAM.font,
        cursor: act ? "pointer" : undefined,
        ...style,
      }}
    >
      {(hasLabel || description || icon) && (
        <div style={{ display: "flex", alignItems: "center", gap: "8px", minWidth: 0, flex: below ? undefined : "1 1 auto" }}>
          {icon && <span style={{ display: "inline-flex", color: STEAM.label }}>{icon}</span>}
          <div style={{ minWidth: 0 }}>
            {hasLabel && <div style={{ color: STEAM.label, fontSize: "16px", lineHeight: "22px", fontWeight: 400 }}>{label}</div>}
            {description && <div style={{ color: STEAM.desc, fontSize: "13px", lineHeight: "17px", marginTop: "2px" }}>{description}</div>}
          </div>
        </div>
      )}
      {children !== undefined && children !== null && children !== false && <div style={childBox}>{children}</div>}
    </div>
  );
});

// Item-based fields (ToggleField, SliderField, DropdownItem, ButtonItem) share these props.
interface ItemProps {
  label?: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  layout?: "below" | "inline";
  icon?: ReactNode;
  bottomSeparator?: "standard" | "thick" | "none";
  indentLevel?: number;
  tooltip?: string;
  highlightOnFocus?: boolean;
}

// ---- toggles ----------------------------------------------------------------------------------

export const Toggle: FC<{ value: boolean; disabled?: boolean; onChange?(v: boolean): void; navLeaf?: boolean }> = ({ value, disabled, onChange, navLeaf = true }) => (
  <div
    role="switch"
    aria-checked={value}
    className={navLeaf ? "pv-ring" : undefined}
    {...navAttrs(navLeaf, disabled)}
    onClick={(e) => {
      e.stopPropagation();
      if (!disabled) onChange?.(!value);
    }}
    onKeyDown={activateKeys(() => !disabled && onChange?.(!value))}
    style={{
      width: "44px",
      height: "24px",
      borderRadius: "12px",
      background: value ? STEAM.blue : "#5a626e",
      position: "relative",
      flexShrink: 0,
      opacity: disabled ? 0.5 : 1,
      cursor: "pointer",
      transition: "background .1s",
    }}
  >
    <div style={{ position: "absolute", top: "3px", left: value ? "23px" : "3px", width: "18px", height: "18px", borderRadius: "50%", background: "#fff" }} />
  </div>
);

export interface ToggleFieldProps extends ItemProps {
  checked: boolean;
  disabled?: boolean;
  onChange?(checked: boolean): void;
}

export const ToggleField: FC<ToggleFieldProps> = ({ label, description, checked, disabled, onChange, icon, bottomSeparator, indentLevel }) => (
  <Field label={label} description={description} icon={icon} bottomSeparator={bottomSeparator} indentLevel={indentLevel} disabled={disabled} onActivate={() => !disabled && onChange?.(!checked)}>
    <Toggle value={checked} disabled={disabled} onChange={onChange} navLeaf={false} />
  </Field>
);

// ---- slider -----------------------------------------------------------------------------------

export interface SliderFieldProps extends ItemProps {
  value: number;
  min?: number;
  max?: number;
  step?: number;
  notchCount?: number;
  notchLabels?: { notchIndex: number; label: string; value?: number }[];
  notchTicksVisible?: boolean;
  showValue?: boolean;
  resetValue?: number;
  disabled?: boolean;
  editableValue?: boolean;
  validValues?: unknown;
  valueSuffix?: string;
  minimumDpadGranularity?: number;
  onChange?(value: number): void;
  className?: string;
}

export const SliderField: FC<SliderFieldProps> = (p) => {
  const min = p.min ?? 0;
  const max = p.max ?? 100;
  const pct = max > min ? Math.max(0, Math.min(100, ((p.value - min) / (max - min)) * 100)) : 0;
  const step = p.step ?? 1;
  const set = (v: number) => p.onChange?.(Math.max(min, Math.min(max, Math.round(v / step) * step)));
  return (
    <div
      className="pv-field pv-ring"
      {...navAttrs(true, p.disabled)}
      data-nav-group="default"
      data-nav-slider="1"
      onKeyDown={(e) => {
        if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
          e.preventDefault();
          e.stopPropagation();
          set(p.value + (e.key === "ArrowLeft" ? -step : step));
        }
      }}
      style={{ boxSizing: "border-box", padding: `10px ${useContext(PanelCtx) ? 0 : 16}px`, borderBottom: p.bottomSeparator === "none" ? "none" : `1px solid ${STEAM.sep}`, fontFamily: STEAM.font, opacity: p.disabled ? 0.5 : 1 }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ color: STEAM.label, fontSize: "16px", lineHeight: "22px" }}>{p.label}</div>
        {p.showValue !== false && (
          <div style={{ color: STEAM.label, fontSize: "14px" }}>
            {p.value}
            {p.valueSuffix ?? ""}
          </div>
        )}
      </div>
      <div
        style={{ position: "relative", height: "24px", marginTop: "4px", cursor: "pointer" }}
        onClick={(e) => {
          const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
          set(min + ((e.clientX - r.left) / r.width) * (max - min));
        }}
      >
        <div style={{ position: "absolute", top: "10px", left: 0, right: 0, height: "4px", borderRadius: "2px", background: "#2b3440" }} />
        <div style={{ position: "absolute", top: "10px", left: 0, width: `${pct}%`, height: "4px", borderRadius: "2px", background: STEAM.blue }} />
        <div style={{ position: "absolute", top: "4px", left: `calc(${pct}% - 8px)`, width: "16px", height: "16px", borderRadius: "50%", background: "#fff" }} />
      </div>
      {p.notchLabels && p.notchLabels.length > 0 && (
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: "12px", color: STEAM.desc }}>
          {p.notchLabels.map((n) => (
            <span key={n.notchIndex}>{n.label}</span>
          ))}
        </div>
      )}
      {p.description && <div style={{ color: STEAM.desc, fontSize: "13px", marginTop: "2px" }}>{p.description}</div>}
    </div>
  );
};

// ---- text -------------------------------------------------------------------------------------

export interface TextFieldProps extends React.HTMLAttributes<HTMLInputElement> {
  label?: ReactNode;
  requiredLabel?: ReactNode;
  description?: ReactNode;
  disabled?: boolean;
  bShowCopyAction?: boolean;
  bShowClearAction?: boolean;
  bAlwaysShowClearAction?: boolean;
  bIsPassword?: boolean;
  rangeMin?: number;
  rangeMax?: number;
  mustBeNumeric?: boolean;
  mustBeURL?: boolean;
  mustBeEmail?: boolean;
  focusOnMount?: boolean;
  tooltip?: string;
  inlineControls?: ReactNode;
  onChange?: (e: ChangeEvent<HTMLInputElement>) => void;
  value?: string;
  [k: string]: any;
}

export const TextField = forwardRef<HTMLInputElement, TextFieldProps>(function TextField(p, ref) {
  const { label, description, disabled, bIsPassword, onChange, value, inlineControls, style } = p;
  return (
    <div style={{ fontFamily: STEAM.font, padding: "6px 0", ...(style as CSSProperties) }}>
      {label && <div style={{ color: STEAM.label, fontSize: "14px", marginBottom: "4px" }}>{label}</div>}
      <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
        <input
          ref={ref}
          className="pv-input"
          data-nav-leaf="1"
          type={bIsPassword ? "password" : "text"}
          disabled={disabled}
          value={value ?? ""}
          onChange={(e) => onChange?.(e)}
          placeholder={typeof p.placeholder === "string" ? p.placeholder : undefined}
          style={{
            flex: 1,
            minWidth: 0,
            boxSizing: "border-box",
            height: "40px",
            padding: "0 12px",
            background: "#1a1f27",
            border: "1px solid #3a414b",
            borderRadius: "2px",
            color: STEAM.text,
            fontFamily: STEAM.font,
            fontSize: "16px",
            outline: "none",
          }}
        />
        {inlineControls}
      </div>
      {description && <div style={{ color: STEAM.desc, fontSize: "13px", marginTop: "4px" }}>{description}</div>}
    </div>
  );
});

// ---- dropdowns --------------------------------------------------------------------------------

export interface SingleDropdownOption {
  data: any;
  label: ReactNode;
  options?: never;
}
export interface MultiDropdownOption {
  label: ReactNode;
  options: DropdownOption[];
  data?: never;
}
export type DropdownOption = SingleDropdownOption | MultiDropdownOption;
export interface DropdownProps {
  rgOptions: DropdownOption[];
  selectedOption: any;
  disabled?: boolean;
  onMenuWillOpen?(showMenu: () => void): void;
  onMenuOpened?(): void;
  onChange?(data: SingleDropdownOption): void;
  contextMenuPositionOptions?: unknown;
  menuLabel?: string;
  strDefaultLabel?: string;
  renderButtonValue?(element: ReactNode): ReactNode;
  focusable?: boolean;
}

function flatOptions(opts: DropdownOption[]): SingleDropdownOption[] {
  const out: SingleDropdownOption[] = [];
  for (const o of opts || []) {
    if ((o as MultiDropdownOption).options) out.push(...flatOptions((o as MultiDropdownOption).options));
    else out.push(o as SingleDropdownOption);
  }
  return out;
}

export const Dropdown: FC<DropdownProps> = ({ rgOptions, selectedOption, disabled, onChange, strDefaultLabel, renderButtonValue, menuLabel }) => {
  const [open, setOpen] = useState(false);
  const opts = flatOptions(rgOptions);
  const sel = opts.find((o) => o.data === selectedOption);
  const shown = sel ? sel.label : strDefaultLabel ?? "";
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <div ref={ref} style={{ position: "relative", minWidth: 0, width: "100%" }}>
      <div
        role="button"
        className="pv-btn pv-ring"
        {...navAttrs(true, disabled)}
        onClick={() => !disabled && setOpen(!open)}
        onKeyDown={activateKeys(() => !disabled && setOpen(!open))}
        style={{
          ...buttonBase,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "8px",
          textAlign: "left",
          fontWeight: 400,
          minWidth: "100px",
          opacity: disabled ? 0.45 : 1,
        }}
      >
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{renderButtonValue ? renderButtonValue(shown) : shown}</span>
        <svg width="12" height="8" viewBox="0 0 12 8" style={{ flexShrink: 0 }}>
          <path d="M1 1l5 5 5-5" stroke="#fff" strokeWidth="2" fill="none" />
        </svg>
      </div>
      {open && (
        <div
          className="pv-menu"
          style={{
            position: "absolute",
            zIndex: 1000,
            top: "100%",
            left: 0,
            minWidth: "100%",
            maxHeight: "360px",
            overflowY: "auto",
            background: "#3d4450",
            boxShadow: "0 8px 24px rgba(0,0,0,.6)",
            borderRadius: "2px",
            padding: "4px 0",
          }}
        >
          {menuLabel && <div style={{ padding: "6px 16px", color: STEAM.desc, fontSize: "13px", textTransform: "uppercase" }}>{menuLabel}</div>}
          {opts.map((o, i) => (
            <div
              key={i}
              data-nav-leaf="1"
              tabIndex={0}
              className="pv-ring"
              onClick={() => {
                setOpen(false);
                onChange?.(o);
              }}
              style={{ padding: "8px 16px", fontSize: "16px", whiteSpace: "nowrap", color: "#fff", background: o.data === selectedOption ? "rgba(255,255,255,.12)" : undefined, cursor: "pointer" }}
            >
              {o.label}
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export interface DropdownItemProps extends DropdownProps, ItemProps {}

export const DropdownItem: FC<DropdownItemProps> = (p) => (
  <Field label={p.label} description={p.description} icon={p.icon} bottomSeparator={p.bottomSeparator} childrenLayout={p.layout === "below" ? "below" : "inline"} childrenContainerWidth="fixed">
    <Dropdown {...p} />
  </Field>
);

// ---- ButtonItem / panels ----------------------------------------------------------------------

export interface ButtonItemProps extends ItemProps {
  onClick?(e: any): void;
  disabled?: boolean;
}

export const ButtonItem: FC<ButtonItemProps> = ({ label, description, children, layout, onClick, disabled, icon, bottomSeparator }) => {
  const below = layout === "below";
  return (
    <Field label={below ? undefined : label} description={below ? undefined : description} icon={below ? undefined : icon} bottomSeparator={bottomSeparator ?? (below ? "none" : "standard")} childrenLayout={below ? "below" : "inline"} padding={below ? "compact" : "standard"}>
      {below && label && <div style={{ color: STEAM.label, fontSize: "16px", marginBottom: "6px" }}>{label}</div>}
      <DialogButton disabled={disabled} onClick={onClick} style={below ? undefined : { width: "auto" }}>
        {children}
      </DialogButton>
      {below && description && <div style={{ color: STEAM.desc, fontSize: "13px", marginTop: "4px" }}>{description}</div>}
    </Field>
  );
};

export const PanelSection: FC<{ title?: string; spinner?: boolean; children?: ReactNode }> = ({ title, children, spinner }) => (
  <PanelCtx.Provider value={true}>
    <div className="pv-panelsection" data-nav-group="default" style={{ padding: "0 16px", marginBottom: "4px", fontFamily: STEAM.font }}>
      {title && (
        <div style={{ display: "flex", alignItems: "center", gap: "8px", padding: "14px 0 6px", fontSize: "13px", fontWeight: 700, letterSpacing: "0.6px", textTransform: "uppercase", color: STEAM.label }}>
          {title}
          {spinner && <Spinner style={{ width: "14px", height: "14px" }} />}
        </div>
      )}
      {children}
    </div>
  </PanelCtx.Provider>
);

export const PanelSectionRow: FC<{ children?: ReactNode }> = ({ children }) => <div className="pv-panelrow">{children}</div>;

// ---- tabs -------------------------------------------------------------------------------------

export interface Tab {
  id: string;
  title: string;
  renderTabAddon?: () => ReactNode;
  content: ReactNode;
  footer?: unknown;
}

export const Tabs: FC<{ tabs: Tab[]; activeTab: string; onShowTab: (tab: string) => void; autoFocusContents?: boolean }> = ({ tabs, activeTab, onShowTab }) => {
  const strip = useRef<HTMLDivElement>(null);
  const active = tabs.find((t) => t.id === activeTab) ?? tabs[0];
  useEffect(() => {
    const el = strip.current?.querySelector<HTMLElement>("[data-active='1']");
    if (el && strip.current) {
      const s = strip.current;
      s.scrollLeft = Math.max(0, el.offsetLeft - (s.clientWidth - el.offsetWidth) / 2);
    }
  }, [activeTab]);
  useEffect(() => {
    // L1 / R1 (bumpers) switch tabs in Steam; Q / E and PageUp / PageDown here.
    const onKey = (e: KeyboardEvent) => {
      const i = tabs.findIndex((t) => t.id === active?.id);
      if (["q", "PageUp"].includes(e.key) && i > 0) onShowTab(tabs[i - 1].id);
      if (["e", "PageDown"].includes(e.key) && i < tabs.length - 1) onShowTab(tabs[i + 1].id);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  return (
    <div className="pv-tabs" style={{ display: "flex", flexDirection: "column", height: "100%", width: "100%", fontFamily: STEAM.font }}>
      <div style={{ display: "flex", alignItems: "center", height: "48px", flexShrink: 0, background: "rgba(0,0,0,.25)", borderBottom: `1px solid ${STEAM.sep}` }}>
        <Glyph text="L1" />
        <div ref={strip} className="pv-tabstrip" style={{ flex: 1, display: "flex", overflow: "hidden", gap: "4px", justifyContent: "safe center", whiteSpace: "nowrap" }}>
          {tabs.map((t) => {
            const on = t.id === active?.id;
            return (
              <div
                key={t.id}
                data-active={on ? "1" : "0"}
                onClick={() => onShowTab(t.id)}
                style={{
                  padding: "0 14px",
                  height: "48px",
                  lineHeight: "48px",
                  fontSize: "14px",
                  fontWeight: 700,
                  letterSpacing: "0.5px",
                  textTransform: "uppercase",
                  color: on ? "#fff" : STEAM.desc,
                  borderBottom: on ? "3px solid #fff" : "3px solid transparent",
                  boxSizing: "border-box",
                  cursor: "pointer",
                  flexShrink: 0,
                }}
              >
                {t.title}
                {t.renderTabAddon?.()}
              </div>
            );
          })}
        </div>
        <Glyph text="R1" />
      </div>
      <div className="pv-tabcontent" data-nav-root="1" data-nav-group="default" style={{ flex: 1, minHeight: 0, overflow: "auto", position: "relative" }}>
        {active?.content}
      </div>
    </div>
  );
};

const Glyph: FC<{ text: string }> = ({ text }) => (
  <div style={{ margin: "0 10px", padding: "2px 6px", borderRadius: "4px", background: "#5a626e", color: "#fff", fontSize: "11px", fontWeight: 700, flexShrink: 0 }}>{text}</div>
);

// ---- modals -----------------------------------------------------------------------------------

export interface ModalRootProps {
  children?: ReactNode;
  onCancel?(): void;
  closeModal?(): void;
  onOK?(): void;
  onEscKeypress?(): void;
  className?: string;
  modalClassName?: string;
  bAllowFullSize?: boolean;
  bDestructiveWarning?: boolean;
  bDisableBackgroundDismiss?: boolean;
  bHideCloseIcon?: boolean;
  bOKDisabled?: boolean;
  bCancelDisabled?: boolean;
}

export const ModalRoot: FC<ModalRootProps> = ({ children, onCancel, closeModal, onEscKeypress, bAllowFullSize, bDisableBackgroundDismiss }) => {
  const cancel = () => {
    onCancel?.();
    onEscKeypress?.();
    closeModal?.();
  };
  useEffect(() => {
    const k = (e: KeyboardEvent) => (e.key === "Escape" || e.key === "Backspace") && !(e.target as HTMLElement)?.matches?.("input") && cancel();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  });
  return (
    <div
      className="pv-modal-backdrop"
      onClick={(e) => e.target === e.currentTarget && !bDisableBackgroundDismiss && cancel()}
      style={{ position: "fixed", inset: 0, zIndex: 5000, background: "rgba(0,0,0,0.72)", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: STEAM.font }}
    >
      <div
        className="pv-modal"
        data-nav-root="1"
        data-nav-group="default"
        style={{
          boxSizing: "border-box",
          width: bAllowFullSize ? "calc(100% - 48px)" : "min(calc(100% - 48px), 760px)",
          maxHeight: "calc(100% - 48px)",
          overflowY: "auto",
          background: "radial-gradient(circle at top left, #3a4250 0%, #23262e 70%)",
          borderRadius: "4px",
          padding: "24px 28px",
          color: STEAM.text,
          boxShadow: "0 12px 48px rgba(0,0,0,.7)",
        }}
      >
        {children}
      </div>
    </div>
  );
};

export interface ConfirmModalProps extends ModalRootProps {
  onMiddleButton?(): void;
  strTitle?: ReactNode;
  strDescription?: ReactNode;
  strOKButtonText?: ReactNode;
  strCancelButtonText?: ReactNode;
  strMiddleButtonText?: ReactNode;
  bAlertDialog?: boolean;
  bMiddleDisabled?: boolean;
}

export const ConfirmModal: FC<ConfirmModalProps> = (p) => {
  const close = () => p.closeModal?.();
  return (
    <ModalRoot {...p}>
      {p.strTitle && <div style={{ fontSize: "24px", fontWeight: 700, marginBottom: "12px", textTransform: "uppercase" }}>{p.strTitle}</div>}
      {p.strDescription && <div style={{ fontSize: "16px", color: STEAM.label, marginBottom: "24px", lineHeight: 1.4 }}>{p.strDescription}</div>}
      {p.children}
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "12px", justifyContent: "flex-end", marginTop: "16px" }}>
        <DialogButton
          disabled={p.bOKDisabled}
          style={{ width: "auto", background: p.bDestructiveWarning ? "#c43c3c" : STEAM.blue }}
          onClick={() => {
            p.onOK?.();
            close();
          }}
        >
          {p.strOKButtonText ?? "Confirm"}
        </DialogButton>
        {p.strMiddleButtonText && (
          <DialogButton
            disabled={p.bMiddleDisabled}
            style={{ width: "auto" }}
            onClick={() => {
              p.onMiddleButton?.();
              close();
            }}
          >
            {p.strMiddleButtonText}
          </DialogButton>
        )}
        {!p.bAlertDialog && (
          <DialogButton
            disabled={p.bCancelDisabled}
            style={{ width: "auto" }}
            onClick={() => {
              p.onCancel?.();
              close();
            }}
          >
            {p.strCancelButtonText ?? "Cancel"}
          </DialogButton>
        )}
      </Focusable>
    </ModalRoot>
  );
};

export const SimpleModal: FC<{ active?: boolean; children: ReactNode }> = ({ children, active }) => (active === false ? null : <div className="pv-simplemodal">{children}</div>);
export const ModalPosition = SimpleModal;

export interface ShowModalResult {
  Close: () => void;
  Update: (modal: ReactNode) => void;
}

let modalSeq = 0;
export function showModal(modal: ReactNode, _parent?: EventTarget, props?: { fnOnClose?: () => void; strTitle?: string }): ShowModalResult {
  const host = document.createElement("div");
  host.className = "pv-modal-host";
  host.dataset.modal = String(++modalSeq);
  document.body.appendChild(host);
  const root: Root = createRoot(host);
  let closed = false;
  const Close = () => {
    if (closed) return;
    closed = true;
    props?.fnOnClose?.();
    setTimeout(() => {
      root.unmount();
      host.remove();
    }, 0);
  };
  const render = (m: ReactNode) => root.render(isValidElement(m) ? cloneElement(m as ReactElement<any>, { closeModal: Close }) : <>{m}</>);
  render(modal);
  (window as any).__wfhModals = ((window as any).__wfhModals ?? 0) + 1;
  return { Close, Update: render };
}

// ---- misc -------------------------------------------------------------------------------------

export const Spinner: FC<{ style?: CSSProperties; className?: string }> = ({ style, className }) => (
  <div className={["pv-spinner", className].filter(Boolean).join(" ")} style={{ width: "24px", height: "24px", border: "3px solid rgba(255,255,255,.2)", borderTopColor: "#fff", borderRadius: "50%", animation: "pv-spin 1s linear infinite", ...style }} />
);
export const SteamSpinner: FC<{ children?: ReactNode; background?: string }> = ({ children }) => (
  <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "12px", padding: "24px" }}>
    <Spinner style={{ width: "48px", height: "48px" }} />
    {children}
  </div>
);

export const ProgressBar: FC<{ nProgress?: number; indeterminate?: boolean }> = ({ nProgress = 0 }) => (
  <div style={{ height: "6px", background: "#2b3440", borderRadius: "3px" }}>
    <div style={{ width: `${nProgress}%`, height: "100%", background: STEAM.blue, borderRadius: "3px" }} />
  </div>
);

export class ErrorBoundary extends Component<{ children?: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: unknown) {
    return { error: String((e as Error)?.message ?? e) };
  }
  componentDidCatch(e: unknown) {
    console.error("[mock ErrorBoundary]", e);
  }
  render() {
    return this.state.error ? <div style={{ color: "#f06464", padding: "16px" }}>Error: {this.state.error}</div> : this.props.children;
  }
}

const navLog = (what: string, ...a: unknown[]) => console.info(`[preview] Navigation.${what}`, ...a);
export const Navigation = {
  Navigate: (path: string) => navLog("Navigate", path),
  NavigateBack: () => navLog("NavigateBack"),
  NavigateToExternalWeb: (url: string) => navLog("NavigateToExternalWeb", url),
  NavigateToLibraryTab: () => navLog("NavigateToLibraryTab"),
  NavigateToSteamWeb: (url: string) => navLog("NavigateToSteamWeb", url),
  OpenQuickAccessMenu: () => navLog("OpenQuickAccessMenu"),
  OpenMainMenu: () => navLog("OpenMainMenu"),
  CloseSideMenus: () => navLog("CloseSideMenus"),
  OpenSideMenu: () => navLog("OpenSideMenu"),
};
export const Router = Navigation;

export const staticClasses: Record<string, string> = new Proxy({} as Record<string, string>, {
  get: (_t, k) => `pv-static-${String(k)}`,
});
export const quickAccessMenuClasses = staticClasses;
export const gamepadDialogClasses = staticClasses;
export const scrollClasses = staticClasses;

// shortcuts.tsx (Steam main-menu / QAM patching) is not rendered by the harness; these keep it importable.
export type Patch = { unpatch: () => void };
export const afterPatch = (_obj: any, _name: string, _fn: any): Patch => ({ unpatch: () => {} });
export const beforePatch = afterPatch;
export const replacePatch = afterPatch;
export const findInReactTree = (_node: any, _filter: (n: any) => boolean): any => null;
export const findInTree = findInReactTree;
export const findModuleByExport = (_filter: (e: any) => boolean): any => undefined;
export const findModuleExport = findModuleByExport;
export const findModule = findModuleByExport;
export const getReactRoot = (_el: any): any => null;
export const createReactTreePatcher = (_filters: any[], _handler: any, _name?: string) => (_args: any, ret: any) => ret;
export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
