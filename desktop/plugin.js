/**
 * AI Usage Tracker — live subscription quota for every AI provider Hermes can
 * route to, per profile, in the Hermes desktop.
 *
 * Personal test extension: persistent native request ledger, provider tabs,
 * cache-rate estimates and native compression telemetry. Quota probes retained.
 *
 * Backend: ~/.hermes/plugins/ai-usage-tracker/dashboard/plugin_api.py
 * Read-only. No secrets ever reach the UI.
 */
import {
  Badge,
  Button,
  Codicon,
  EmptyState,
  ErrorState,
  ROUTES_AREA,
  STATUSBAR_AREAS,
  SIDEBAR_NAV_AREA,
  ScrollArea,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Separator,
  Skeleton,
  atom,
  cn,
  haptic,
  host,
  useQuery,
  useValue,
  queryClient
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState, useEffect, useRef, createElement as h } from 'react'

const ID = 'ai-usage-tracker'
const ROUTE = '/ai-usage'
const REFRESH_PAGE_MS = 120_000
const REFRESH_CHIP_MS = 300_000

let socket = null
let rest = null
let storage = null
let pluginOs = null
let pendingRefresh = false

// Providers the user has hidden — plugin-scoped, shared by page and chip.
const HIDDEN_KEY = 'hidden-providers-v1'
const $hidden = atom([])

// Selected profile — plugin-scoped, shared by page and chip. Empty means
// "whatever this backend runs as".
const PROFILE_KEY = 'selected-profile-v1'
const $profile = atom('')
// A tagged scope cannot collide with a real profile named "all" (or any name).
const ALL_PROFILES = Object.freeze({profile_scope:'all'})
const PROFILE_SCOPE_KEY = 'selected-profile-scope-v1'
const isAllProfiles = value => value?.profile_scope === 'all'
const scopeParams = value => isAllProfiles(value) ? {profile_scope:'all'} : {profile:value||''}
const pickerValue = value => isAllProfiles(value) ? 'scope:all' : 'profile:'+value
let discoveredProfiles = null
async function scopedRead(path,options){
 const aggregate=new URLSearchParams(path.split('?')[1]).get('profile_scope')==='all';
 // Older routes can silently ignore unknown query parameters. Discover support
 // before sending an aggregate request, especially to the quota-probing route.
 if(aggregate){
  const inventory=discoveredProfiles||await sharedLedgerRead('/ledger/profiles');
  if(!inventory?.scope_options?.some(v=>v.profile_scope==='all'))throw new Error('All profiles requires a user-managed backend restart.');
 }
 const data=await rest(path,options);
 if(path==='/ledger/profiles')discoveredProfiles=data;
 if(aggregate&&(data?.profile_scope!=='all'||data?.read_only!==true))throw new Error('All profiles is not supported by this backend response. A user-managed backend restart is required.');
 return data;
}
const originalId = (row,key) => row?.original_ids?.[key] ?? row?.[key]
const provenance = row => row?.profile ? ' · '+row.profile : ''
const readable = (row,key) => String(originalId(row,key) ?? '—')+provenance(row)
const AGGREGATE_QUERY_OPTIONS = {retry:false,refetchInterval:60000,refetchIntervalInBackground:false,refetchOnWindowFocus:false,refetchOnReconnect:false}
function readOptions(scope){return isAllProfiles(scope)?AGGREGATE_QUERY_OPTIONS:CONNECTION_QUERY_OPTIONS}
function rollingPath(path,seconds){
 if(!seconds)return path;
 const [route,search]=path.split('?'),p=new URLSearchParams(search);
 p.set('start',String(Math.max(0,Date.now()/1000-seconds)));return route+'?'+p;
}
function Coverage({data}){
 const c=data?.coverage;if(data?.profile_scope!=='all'||!c)return null;
 return h('div',{className:'au-muted',role:'status','data-testid':'profile-coverage',title:c.note},
  'Profile coverage: '+c.status+' · '+count(c.read_profiles)+' / '+count(c.selected_profiles)+' readable',
  h('details',{},h('summary',{},'Coverage details'),h('div',{},c.note),...(c.profiles||[]).map(p=>h('div',{key:p.profile_id},p.name+': '+p.status+(p.reason?' · '+p.reason:'')))));
}

// Which provider the status-bar chip watches. Empty = AUTO: the worst remaining
// window across every visible provider (the historical default). A provider id
// pins the chip to that provider's own worst window; the page writes it.
const CHIP_KEY = 'chip-provider-v1'
const AUTO_CHIP = 'auto'
const $chipProvider = atom('')

function selectChipProvider(id) {
  const value = String(id || '')
  $chipProvider.set(value)
  try {
    storage?.set(CHIP_KEY, value)
  } catch {
    /* in-memory selection still works for this session */
  }
}

function persistHidden(ids) {
  $hidden.set(ids)
  try {
    storage?.set(HIDDEN_KEY, ids)
  } catch {
    /* keep the in-memory list when storage is unavailable */
  }
}

function hideProvider(id) {
  const current = $hidden.get()
  if (!current.includes(id)) persistHidden([...current, id])
}

function unhideProvider(id) {
  persistHidden($hidden.get().filter(value => value !== id))
}

function unhideAll() {
  persistHidden([])
}

function selectProfile(name) {
  const value = isAllProfiles(name) ? ALL_PROFILES : String(name || '')
  $profile.set(value)
  try {
    storage?.set(PROFILE_SCOPE_KEY, isAllProfiles(value) ? 'all' : 'selected')
    if (!isAllProfiles(value)) storage?.set(PROFILE_KEY, value)
  } catch {
    /* in-memory selection still works for this session */
  }
}

// ---------------------------------------------------------------- formatters

function fmtIst(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: true
  })
}

function resetLabel(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  const seconds = Math.round((d.getTime() - Date.now()) / 1000)
  if (seconds <= 0) return 'resets now'
  const days = Math.floor(seconds / 86_400)
  const hours = Math.floor((seconds % 86_400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const rel = days ? `in ${days}d ${hours}h` : hours ? `in ${hours}h ${minutes}m` : `in ${minutes}m`
  return `resets ${rel} · ${fmtIst(iso)} IST`
}

function pct(value) {
  if (value === null || value === undefined) return '—'
  return `${Math.round(Number(value))}%`
}

function toneFor(remaining) {
  if (remaining === null || remaining === undefined) return 'muted'
  if (remaining <= 5) return 'bad'
  if (remaining <= 25) return 'warn'
  return 'good'
}

function statusOf(provider) {
  if (provider.quota?.available) return { variant: 'success', label: 'live quota' }
  if (provider.quota?.unavailable_reason) return { variant: 'muted', label: 'no quota' }
  return { variant: 'muted', label: 'unavailable' }
}

function profileLabel(row) {
  if (!row) return 'profile'
  return `${row.name || 'Server profile'}${row.is_default ? ' · default' : ''}${row.gateway_running ? ' · up' : ''}`
}

/** Worst (lowest) remaining percent across visible providers' windows — drives the chip. */
/** One provider's tightest numeric window, or null when it reports none. */
function worstWindowOf(provider) {
  let worst = null
  for (const window of provider?.quota?.windows || []) {
    const remaining = window.remaining_percent
    if (remaining === null || remaining === undefined) continue
    if (worst === null || remaining < worst.remaining) {
      worst = {
        remaining,
        provider: provider.label,
        window: window.label,
        reset_at: window.reset_at || null
      }
    }
  }
  return worst
}

/** AUTO pick: the tightest window across every provider that isn't hidden. */
function worstRemaining(payload, hiddenIds) {
  const hidden = hiddenIds || []
  let worst = null
  for (const provider of payload?.providers || []) {
    if (hidden.includes(provider.id)) continue
    const candidate = worstWindowOf(provider)
    if (candidate && (worst === null || candidate.remaining < worst.remaining)) worst = candidate
  }
  return worst
}

// ---------------------------------------------------------------- data hooks

function usagePath(profile, refresh) {
  const params = []
  if (isAllProfiles(profile)) params.push('profile_scope=all')
  else if (profile) params.push(`profile=${encodeURIComponent(profile)}`)
  if (refresh) params.push('refresh=1')
  return params.length ? `/usage?${params.join('&')}` : '/usage'
}

function useUsage(profile, intervalMs) {
  return useQuery({
    queryKey: ['ai-usage-tracker', 'usage', profile || 'server'],
    queryFn: () => {
      const refresh = pendingRefresh
      pendingRefresh = false
      return scopedRead(usagePath(profile, refresh))
    },
    refetchInterval: intervalMs,
    ...(isAllProfiles(profile) ? AGGREGATE_QUERY_OPTIONS : {}),
    refetchOnMount: 'always',
    refetchOnWindowFocus: !isAllProfiles(profile),
    staleTime: 30_000,
    retry: isAllProfiles(profile) ? false : 1,
    // Discovery owns the picker; never carry quota across profile identities.
    placeholderData: undefined
  })
}

// ---------------------------------------------------------------- components

function QuotaBar({ window }) {
  const remaining = window.remaining_percent
  if (remaining === null || remaining === undefined) {
    return jsxs('div', {
      className: 'flex items-baseline gap-2 text-xs',
      children: [
        jsx('span', { className: 'w-28 shrink-0 text-(--ui-text-tertiary)', children: window.label }),
        jsx('span', { className: 'text-(--ui-text-quaternary)', children: window.detail || 'unlimited' })
      ]
    })
  }
  const tone = toneFor(remaining)
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-baseline gap-2 text-xs',
        children: [
          jsx('span', { className: 'w-28 shrink-0 text-(--ui-text-tertiary)', children: window.label }),
          jsx('span', {
            className: cn('font-medium tabular-nums', remaining <= 25 ? 'text-(--ui-text-primary)' : 'text-(--ui-text-secondary)'),
            children: `${pct(remaining)} left`
          }),
          window.detail ? jsx('span', { className: 'text-(--ui-text-quaternary)', children: window.detail }) : null,
          resetLabel(window.reset_at)
            ? jsx('span', { className: 'text-(--ui-text-quaternary)', children: resetLabel(window.reset_at) })
            : null,
          tone === 'bad' || tone === 'warn'
            ? jsx(Badge, { variant: tone === 'bad' ? 'destructive' : 'warn', size: 'xs', children: tone === 'bad' ? 'low' : 'watch' })
            : null
        ]
      }),
      jsx('div', {
        className: 'h-1.5 w-full overflow-hidden rounded-[2px]',
        style: { background: 'var(--ui-stroke-secondary)' },
        children: jsx('div', {
          className: 'h-full rounded-[2px]',
          style: { width: `${Math.max(1, Math.min(100, remaining))}%`, background: 'var(--ui-accent)' }
        })
      })
    ]
  })
}

// All reset controls live in the card header. The same component is used on
// Subscriptions and the Codex provider page; query identity includes profile.
const RESET_WARNING = 'Auto use is opt-in and at your own risk. It can use a banked reset at the five-hour limit on Plus accounts, not only at the weekly limit. A reset refreshes eligible five-hour and weekly allowances; an early use can waste the weekly reset.'
function CodexResetControls({profile}) {
  const [open,setOpen] = useState(false), [confirm,setConfirm] = useState(false)
  const [busy,setBusy] = useState(false), [message,setMessage] = useState('')
  const badgeRef=useRef(null),useRefButton=useRef(null),dialogRef=useRef(null),confirmed=useRef(null)
  const path = '/codex/resets?profile='+encodeURIComponent(profile)
  const query = useQuery({queryKey:[ID,'codex-resets',profile],queryFn:()=>rest(path),
    retry:false,refetchInterval:60000,refetchIntervalInBackground:false,refetchOnWindowFocus:false})
  const state = query.data?.profile === profile && !query.isPlaceholderData ? query.data : null
  const count = Number.isSafeInteger(state?.count) && state.count >= 0 ? state.count : null
  const blocked = !!state?.blocked
  const eligible = count > 0 && state?.redeemable === true && !blocked && !query.error
  const confirmationValid=eligible && confirmed.current?.profile===profile &&
    confirmed.current?.binding===state?.binding && confirmed.current?.episode===state?.episode &&
    confirmed.current?.count===state?.count
  function restoreFocus(){(useRefButton.current && !useRefButton.current.disabled ? useRefButton.current : badgeRef.current)?.focus()}
  useEffect(()=>{if(confirm && !confirmationValid){setConfirm(false);restoreFocus()}},
    [confirm,confirmationValid])
  function closeConfirm(){setConfirm(false);restoreFocus()}
  function dialogKeys(e){
    if(e.key==='Escape'){e.preventDefault();e.stopPropagation();closeConfirm()}
    if(e.key==='Tab'){
      const buttons=Array.from(dialogRef.current?.querySelectorAll('button:not(:disabled)')||[])
      if(!buttons.length)return
      const edge=e.shiftKey?buttons[0]:buttons[buttons.length-1]
      if(document.activeElement===edge){e.preventDefault();(e.shiftKey?buttons[buttons.length-1]:buttons[0]).focus()}
    }
  }
  const tone = count === null || count === 0 ? 'zero' : eligible ? 'ready' : 'waiting'
  const reason = blocked ? 'Redemption status is uncertain. No second attempt is allowed; review your Codex account before trying again.'
    : query.error ? 'Account-bound balance unavailable: '+String(query.error.message||query.error)
    : count === null ? 'Balance or account identity is unknown; redemption is disabled.'
    : count === 0 ? 'No banked resets available.'
    : !eligible ? 'No provider-confirmed five-hour or weekly exhaustion; rounded 0% is not enough.' : 'An eligible limit was confirmed by Codex.'
  async function auto(e) {
    if(!state?.binding || busy)return
    const enabled=e.currentTarget.checked
    setBusy(true);setMessage('')
    try {
      const result=await rest('/codex/resets/auto',{method:'POST',body:{profile,binding:state.binding,enabled}})
      if(result.profile!==profile)throw new Error('Wrong profile response')
      await query.refetch({cancelRefetch:false})
    }catch(err){setMessage(String(err.message||err));await query.refetch({cancelRefetch:false})}
    finally{setBusy(false)}
  }
  async function redeem() {
    if(!confirmationValid || busy)return
    setBusy(true);setConfirm(false);setMessage('')
    badgeRef.current?.focus()
    try {
      // Never use a cached window as redemption authority. The backend reads
      // again under its account-scoped exclusive operation before consumption.
      const result=await rest('/codex/resets/redeem',{method:'POST',body:{profile,
        binding:state.binding,episode:state.episode,count:state.count}})
      if(result.profile!==profile)throw new Error('Wrong profile response')
      setMessage(result.outcome==='reset'||result.outcome==='alreadyRedeemed'
        ? 'Reset applied; Codex limits were read back.' : 'Codex returned '+result.outcome+'; no reset was applied.')
    }catch(err){setMessage(String(err.message||err))}
    finally{setBusy(false);await query.refetch({cancelRefetch:false})}
  }
  return jsxs('div',{className:'au-reset-header','data-testid':'codex-resets',children:[
    jsxs('label',{className:'au-reset-auto',children:[jsxs('span',{children:[
      jsx('input',{type:'checkbox','aria-label':'Auto use banked Codex reset',
        'aria-describedby':'au-reset-auto-warning',checked:!!state?.auto,
        disabled:!state?.binding||!!query.error||busy||blocked,onChange:auto}), ' Auto use']}),
      jsx('span',{id:'au-reset-auto-warning',className:'au-reset-tooltip',role:'tooltip',children:RESET_WARNING})]}),
    jsx('button',{type:'button',className:'au-reset-badge',
      'data-tone':tone,'aria-label':'Resets: '+(count===null?'unknown':count),
      title:reason,ref:node=>{badgeRef.current=node},onClick:()=>{setOpen(!open);setConfirm(false);confirmed.current=null},children:'Resets: '+(count===null?'—':count)}),
    open?jsxs('div',{className:'au-reset-popover',role:'group','aria-label':'Codex banked resets',children:[
      jsx('div',{children:count===null?'Banked reset balance unavailable.':count+' banked reset'+(count===1?'':'s')+' available.'}),
      jsx('div',{className:'au-reset-reason',children:reason}),
      jsx('span',{title:eligible?'Redeem one banked reset':reason,children:
        jsx('button',{type:'button',ref:node=>{useRefButton.current=node},disabled:!eligible||busy,title:eligible?'Redeem one banked reset':reason,
          className:!eligible?'au-reset-blocked':'',onClick:()=>{confirmed.current={profile,binding:state.binding,episode:state.episode,count:state.count};setConfirm(true)},children:busy?'Checking…':'Use one reset'})}),
      confirm && confirmationValid?jsxs('div',{ref:node=>{if(node && !dialogRef.current)node.querySelector('button')?.focus();dialogRef.current=node},role:'alertdialog','aria-label':'Confirm banked reset','aria-modal':true,onKeyDown:dialogKeys,children:[
        jsx('p',{children:'Spend one banked reset? This refreshes eligible five-hour and weekly allowances and can change the weekly reset date.'}),
        jsx('button',{type:'button',onClick:redeem,disabled:busy||!confirmationValid,children:'Confirm use'}),
        jsx('button',{type:'button',onClick:closeConfirm,children:'Cancel'})]}):null,
      message?jsx('p',{role:'status',children:message}):null,
      jsx('button',{type:'button',onClick:()=>query.refetch({cancelRefetch:false}),disabled:busy,children:'Refresh balance'})
    ]}):null
  ]})
}

function ProviderCard({ provider, isHidden, onNavigate, profile }) {
  const status = statusOf(provider)
  const windows = provider.quota?.windows || []
  const details = provider.quota?.details || []

  return jsxs('div', {
    className: cn(
      'au-quota-card flex flex-col gap-2 rounded-[5px] border border-(--ui-stroke-secondary) p-3',
      isHidden && 'opacity-60'
    ),
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          onNavigate
            ? jsx('button', {
                type: 'button',
                className: 'au-provider-name text-sm font-medium',
                'aria-label': `Open ${provider.label} provider page`,
                onClick: () => { haptic('tap'); onNavigate(provider.id) },
                children: provider.label
              })
            : jsx('span', { className: 'au-provider-name-static text-sm font-medium', children: provider.label }),
          provider.quota?.plan
            ? jsx(Badge, { variant: 'muted', size: 'xs', children: provider.quota.plan })
            : null,
          jsx(Badge, { variant: status.variant, size: 'xs', children: status.label }),
          provider.active
            ? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: 'used recently' })
            : null,
          jsx('span', { className: 'flex-1' }),
          provider.id === 'openai-codex' && profile ? jsx(CodexResetControls,{profile}) : null,
          jsx('button', {
            type: 'button',
            title: isHidden ? 'Show this provider again' : 'Hide this provider',
            'aria-label': isHidden ? `Unhide ${provider.label}` : `Hide ${provider.label}`,
            className: 'shrink-0 px-1 text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)',
            onClick: () => {
              haptic('tap')
              if (isHidden) unhideProvider(provider.id)
              else hideProvider(provider.id)
            },
            children: jsx(Codicon, { name: isHidden ? 'eye' : 'close' })
          })
        ]
      }),
      jsxs('div', {
        className: 'au-quota-rows',
        'data-columns': windows.length + details.length >= 6 ? 'multiple' : 'single',
        children: [
          ...windows.map((window, index) => jsx(QuotaBar, { window }, `window-${index}`)),
          ...details.map((detail, index) =>
            jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: detail }, `detail-${index}`)
          )
        ]
      }),
      !provider.quota?.available && provider.quota?.unavailable_reason
        ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: provider.quota.unavailable_reason })
        : null
    ]
  })
}

function ProfilePicker({ profiles, value, onSelect }) {
  if(!isAllProfiles(value)&&!profiles.some(p=>p.name===(value||'')))profiles=[...profiles,{name:value||'',is_server:true}];
  return jsxs(Select, {
    value: pickerValue(value || ''),
    onValueChange: next => onSelect(next === 'scope:all' ? ALL_PROFILES : next.slice('profile:'.length)),
    children: [
      jsx(SelectTrigger, {
        className: 'h-6 w-44 text-[0.6875rem]',
        'aria-label': 'Hermes profile',
        children: jsx(SelectValue, { placeholder: 'profile…' })
      }),
      jsx(SelectContent, {
        children: [jsx(SelectItem, {value:'scope:all',children:'All profiles'}, 'scope:all'),
          ...profiles.map(row => jsx(SelectItem, { value: 'profile:'+row.name, children: profileLabel(row) }, row.name))]
      })
    ]
  })
}

/** Picks which provider the status-bar chip watches. AUTO_CHIP keeps the
 *  historical behaviour (worst window across all visible providers); hidden
 *  providers aren't offered because the chip skips them either way. */
function ChipPicker({ providers, value, onSelect }) {
  return jsxs(Select, {
    value: value || AUTO_CHIP,
    onValueChange: next => {
      haptic('tap')
      onSelect(next === AUTO_CHIP ? '' : next)
    },
    children: [
      jsx(SelectTrigger, {
        className: 'h-6 w-56 text-[0.6875rem]',
        title: 'Which provider the status-bar chip shows',
        'aria-label': 'Status bar chip provider',
        children: jsx(SelectValue, {})
      }),
      jsx(SelectContent, {
        children: [
          jsx(SelectItem, { value: AUTO_CHIP, children: 'Auto (Lowest %)' }, AUTO_CHIP),
          ...providers.map(provider =>
            jsx(SelectItem, { value: provider.id, children: provider.label }, provider.id)
          )
        ]
      })
    ]
  })
}

function PageHeader({ profiles, profile, setProfile, chipProviders, chipProvider, setChipProvider, isFetching, refetch, meta, hiddenCount, showHidden, setShowHidden, refreshMenu, refreshBusy }) {
  return jsxs('div', {
    className: 'au-header-main',
    children: [
      jsx('span', { className: 'au-header-title text-sm font-medium', title: meta || '', children: 'AI usage +' }),
      jsx('span', {
        className: 'au-header-meta text-[0.6875rem] text-(--ui-text-quaternary)',
        title: meta || '',
        children: meta || ''
      }),
      hiddenCount > 0
        ? jsxs(Button, {
            variant: 'ghost',
            title: showHidden ? 'Hide the hidden providers again' : `Show ${hiddenCount} hidden provider(s)`,
            onClick: () => {
              haptic('tap')
              setShowHidden(!showHidden)
            },
            children: [
              jsx(Codicon, { name: 'eye' }),
              jsx('span', { children: showHidden ? `Showing hidden ${hiddenCount}` : `Hidden ${hiddenCount}` })
            ]
          })
        : null,
      showHidden && hiddenCount > 0
        ? jsx(Button, {
            variant: 'text',
            onClick: () => {
              haptic('tap')
              unhideAll()
            },
            children: 'Unhide all'
          })
        : null,
      jsxs('div', { className: 'au-header-chip', children: [
        jsx('span', { className: 'au-header-chip-label text-[0.6875rem] text-(--ui-text-quaternary)', children: 'Status Bar:' }),
        jsx('div', { className: 'au-header-picker', children: jsx(ChipPicker, { providers: chipProviders, value: chipProvider, onSelect: setChipProvider }) })
      ] }),
      jsx('div', { className: 'au-header-profile au-header-picker', children: jsx(ProfilePicker, { profiles, value: profile, onSelect: setProfile }) }),
      jsxs('div', { className: 'au-refresh-split', children: [jsxs(Button, {
        variant: 'secondary',
        title: isFetching ? 'Refreshing data' : 'Refresh data',
        'aria-label': isFetching ? 'Refreshing…' : 'Refresh',
        onClick: () => {
          haptic('tap')
          pendingRefresh = true
          refetch()
        },
        disabled: isFetching || refreshBusy,
        children: [jsx(Codicon, { name: 'refresh' }), jsx('span', { children: isFetching ? 'Refreshing…' : 'Refresh' })]
      }), refreshMenu] })
    ]
  })
}

// ---- Request ledger UI. Provider navigation first; summary before detail tabs. ----
const ledgerCss = `
/* Shared with the standalone preview. Hermes ui-bg-primary is an accent fill,
   NOT the page background. Keep card elevation and segmented-control tracks
   distinct; do not change the host's page background or horizontal padding. */
.au-ledger { container-type:inline-size; color:var(--ui-text-primary);font-size:.8125rem;
 --au-surface-bg:var(--ui-bg-chrome,var(--dt-background,#11111e));
 --au-card-bg:#1c1c28;--au-table-bg:#171723;--au-table-head-bg:#1c1c2b;
 --au-control-bg:#212132;--au-selected-bg:#2b2543;--au-hover-bg:#262437;
}
@supports (background-color:color-mix(in srgb,black,white)){
 .au-ledger{
  --au-card-bg:color-mix(in srgb,var(--ui-base,#d4d1e4) 6%,var(--au-surface-bg));
  --au-table-bg:color-mix(in srgb,var(--ui-base,#d4d1e4) 3%,var(--au-surface-bg));
  --au-table-head-bg:color-mix(in srgb,var(--ui-base,#d4d1e4) 7%,var(--au-surface-bg));
  --au-control-bg:color-mix(in srgb,var(--ui-base,#d4d1e4) 8%,var(--au-surface-bg));
  --au-selected-bg:color-mix(in srgb,var(--ui-accent,#a799ef) 20%,var(--au-surface-bg));
  --au-hover-bg:color-mix(in srgb,var(--ui-accent,#a799ef) 8%,var(--au-surface-bg));
 }
}
.au-ledger *{box-sizing:border-box}.au-ledger .au-muted{color:var(--ui-text-tertiary);font-size:.75rem;line-height:1.6}
.au-ledger .au-box{background:var(--au-table-bg);border:1px solid var(--ui-stroke-secondary);border-radius:10px;padding:16px;margin:12px 0}
.au-ledger .au-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px;margin-top:12px}
.au-ledger .au-metric{background:var(--au-card-bg);border:1px solid var(--ui-stroke-secondary);padding:12px;border-radius:8px;min-width:0}
.au-ledger .au-number{font-size:1.4375rem;font-weight:650;margin:6px 0;letter-spacing:-.6px}
.au-ledger .au-toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:12px 0}
.au-ledger button,.au-ledger select,.au-ledger input{font:inherit;color:var(--ui-text-primary);background:var(--au-control-bg);border:1px solid var(--ui-stroke-secondary);border-radius:6px;padding:7px 10px;max-width:100%}
.au-ledger button{cursor:pointer}.au-ledger button:disabled{opacity:.5;cursor:default}
.au-ledger .au-tabs{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0;padding-bottom:12px;border-bottom:1px solid var(--ui-stroke-secondary)}
.au-ledger .au-table{background:var(--au-table-bg);width:100%}
.au-ledger table{border-collapse:collapse;font-size:.75rem;width:100%;text-align:left;white-space:nowrap}
.au-ledger th,.au-ledger td{padding:10px 12px;border-bottom:1px solid var(--ui-stroke-secondary);vertical-align:top}
.au-ledger th{background:var(--au-table-head-bg);color:var(--ui-text-secondary);font-weight:600}
.au-ledger td:hover{background:var(--au-hover-bg)}
.au-ledger pre{background:var(--au-surface-bg);white-space:pre-wrap;word-break:break-word;max-width:680px;font-size:.6875rem;max-height:400px;overflow:auto}
.au-ledger .au-json-details{min-width:145px}
.au-ledger .au-json-details>summary{cursor:pointer;white-space:nowrap}
.au-ledger .au-json-panel{position:relative;background:var(--au-surface-bg);border:1px solid var(--ui-stroke-secondary);border-radius:7px;margin-top:8px;width:min(660px,75vw);max-width:660px;min-width:240px;overflow:hidden}
.au-ledger .au-json-toolbar{display:flex;justify-content:flex-end;align-items:center;gap:8px;padding:4px 6px;min-height:28px}
.au-ledger .au-json-copy-status{font-size:.6875rem;color:var(--ui-text-secondary)}
.au-ledger .au-json-copy,.au-ledger .au-json-copy:hover,.au-ledger .au-json-copy:active{display:inline-flex;align-items:center;justify-content:center;flex:none;width:28px;height:28px;padding:4px;background:transparent;border:0;box-shadow:none;color:var(--ui-text-secondary);cursor:pointer}
.au-ledger .au-json-copy:hover{color:var(--ui-accent)}
.au-ledger .au-json-copy svg{display:block}
.au-ledger pre.au-json-text{box-sizing:border-box;padding:8px 12px 14px;margin:0;max-height:400px;max-width:100%;width:100%;line-height:1.6;white-space:pre-wrap;overflow:auto;overflow-wrap:anywhere;cursor:text;user-select:text!important;-webkit-user-select:text!important}
.au-ledger .au-json-text code{font:inherit;user-select:text!important;-webkit-user-select:text!important}
@container(max-width:620px){.au-ledger .au-json-panel{width:75vw;min-width:220px}.au-ledger pre.au-json-text{max-height:340px}}

.au-ledger .au-provider-select{display:none}.au-ledger .au-notice{border-left:3px solid var(--ui-accent);padding:9px 13px;margin:12px 0;color:var(--ui-text-secondary);line-height:1.6}
.au-ledger .au-meter{height:6px;width:110px;background:var(--ui-stroke-secondary);border-radius:3px;margin:5px 0;overflow:hidden}
.au-ledger .au-meter > i{display:block;height:100%;background:var(--ui-accent)}
.au-ledger .au-page-header{display:flex;align-items:center;gap:10px;flex-wrap:nowrap}
.au-ledger .au-original-header{flex:1;min-width:0}
.au-header-main{display:flex;align-items:center;flex-wrap:nowrap;gap:8px;min-width:0}
.au-header-title,.au-header-chip-label{white-space:nowrap;flex-shrink:0}
.au-header-meta{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.au-header-chip{display:flex;align-items:center;gap:8px;flex:0 1 300px;min-width:90px}
.au-header-picker{min-width:0;flex:1 1 auto}
.au-header-profile{flex:0 1 176px;min-width:70px}
.au-ledger .au-header-picker>:is(button,select){width:100%;min-width:0;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.au-header-main>.au-refresh-split,.au-page-header>.au-connection{flex-shrink:0}
.au-header-main>button{white-space:nowrap;flex-shrink:0}
@container(max-width:1000px){.au-header-chip-label{display:none}.au-header-chip{flex-basis:224px}}
@container(max-width:650px){
 .au-header-meta{display:none}.au-header-main{gap:5px}.au-ledger .au-page-header{gap:5px}
 .au-header-chip{min-width:60px}.au-header-profile{min-width:55px}
 .au-header-main .au-refresh-split>button span:last-child{display:none}
 .au-header-main>button{max-width:40px;overflow:hidden;text-overflow:ellipsis}
}
@container(max-width:400px){
 .au-header-title{font-size:.6875rem}.au-page-header>.au-connection span:last-child{display:none}
 .au-header-chip,.au-header-profile{min-width:0}
 .au-ledger .au-page-header>.au-connection{padding:7px}
 .au-ledger .au-header-picker>:is(button,select){padding-left:5px;padding-right:5px}
}
.au-ledger .au-connection{display:inline-flex;align-items:center;gap:6px;padding:5px 9px;font-size:.6875rem;line-height:1.4;white-space:nowrap;border-radius:999px;background:transparent;flex:none}
.au-ledger .au-connection-dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.au-ledger .au-connection[data-state="online"]{color:var(--ui-text-success,#64bba8);border-color:var(--ui-stroke-success,#2d514b)}
.au-ledger .au-connection[data-state="limited"],.au-ledger .au-connection[data-state="unverified"]{color:var(--ui-text-warning,#d2b776);border-color:var(--ui-stroke-warning,#5b5135)}
.au-ledger .au-connection[data-state="disconnected"],.au-ledger .au-connection[data-state="not_recording"]{color:var(--ui-text-error,#d58c96);border-color:var(--ui-stroke-error,#613f49)}
.au-ledger .au-connection[data-state="checking"]{color:var(--ui-text-warning,#d2b776);border-color:var(--ui-stroke-warning,#5b5135)}
.au-ledger .au-connection[aria-busy="true"] .au-connection-dot{width:10px;height:10px;background:transparent;border:2px solid currentColor;border-right-color:transparent;animation:au-connection-spin .8s linear infinite}
@keyframes au-connection-spin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){.au-ledger .au-connection[aria-busy="true"] .au-connection-dot{animation:none}}
.au-refresh-split{display:inline-flex;align-items:center;gap:2px}
.au-refresh-menu{position:relative}
.au-refresh-menu>summary{cursor:pointer;padding:7px 9px;list-style:none;border:1px solid var(--ui-stroke-secondary);border-radius:6px;background:var(--au-control-bg)}
.au-refresh-menu>summary::-webkit-details-marker{display:none}
.au-refresh-options{position:absolute;right:0;top:100%;z-index:20;min-width:220px;padding:5px;background:var(--au-card-bg);border:1px solid var(--ui-stroke-secondary);border-radius:6px;display:grid;gap:4px}
.au-ledger .au-refresh-options button{text-align:left;border:0}

.au-ledger [data-testid="cache-costs"] tfoot th,.au-ledger [data-testid="cache-costs"] tfoot td{font-weight:650;border-top:2px solid var(--ui-stroke-secondary);background:var(--au-table-head-bg)}
.au-ledger .au-cache-window{display:flex;align-items:center;flex-wrap:wrap;gap:8px 12px;margin:12px 0}
.au-ledger .au-cache-window .au-segment{margin:0}

.au-ledger .au-component-cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:12px 0}
.au-ledger .au-cost-card{background:var(--au-card-bg);border:1px solid var(--ui-stroke-secondary);border-radius:8px;padding:12px;min-width:0}
.au-ledger .au-cost-card .au-number{font-size:1.4375rem;overflow-wrap:anywhere}
.au-ledger .au-cost-card dl{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 9px;margin:12px 0 0;font-size:.6875rem;line-height:1.5}
.au-ledger .au-cost-card dt{margin:0;color:var(--ui-text-tertiary)}
.au-ledger .au-cost-card dd{margin:0;text-align:right;color:var(--ui-text-primary);font-variant-numeric:tabular-nums}
.au-ledger .au-read-growth{margin-top:10px;padding-top:8px;border-top:1px solid var(--ui-stroke-secondary);font-size:.6875rem;color:var(--ui-text-tertiary);line-height:1.5}
.au-ledger .au-read-growth strong{font-weight:600;color:var(--ui-text-secondary)}
@container(max-width:1100px){.au-ledger .au-component-cards{grid-template-columns:repeat(3,minmax(0,1fr))}}
@container(max-width:620px){.au-ledger .au-component-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.au-ledger .au-cost-card .au-number{font-size:1.25rem}}
@container(max-width:370px){.au-ledger .au-component-cards{grid-template-columns:1fr}}

.au-ledger .au-rate-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:12px 0}
@container (max-width:620px){.au-ledger .au-provider-tabs{display:none}.au-ledger .au-provider-select{display:block}.au-ledger .au-number{font-size:1.25rem}}
.au-ledger{color-scheme:dark;font-family:inherit}
.au-ledger .au-usage-summary{padding:0 0 14px;border-bottom:1px solid var(--ui-stroke-secondary);margin-top:6px}
.au-ledger .au-hero{display:grid;grid-template-columns:minmax(240px,0.8fr) minmax(360px,1.7fr);gap:48px;align-items:start;margin:20px 0 25px}
.au-ledger .au-big{font-size:3rem;letter-spacing:-1.8px;font-weight:650;line-height:1.2;margin:4px 0 8px}
.au-ledger .au-provider-totals{margin-top:28px}.au-ledger .au-provider-row{display:grid;grid-template-columns:1fr auto;gap:8px;margin:17px 0}
.au-ledger .au-provider-row small{font-size:.6875rem;color:var(--ui-text-tertiary);font-weight:400}.au-ledger .au-provider-row .au-muted{grid-column:1 / -1}
.au-ledger .au-provider-cost{font-size:1.5rem;line-height:1.1;font-weight:700;color:var(--ui-text-primary)}
.au-ledger .au-timeline-entry{background:var(--au-table-bg);border:1px solid var(--ui-stroke-secondary);border-radius:8px;margin:8px 0;overflow-wrap:anywhere}
.au-ledger .au-timeline-scroll{padding-right:4px}
.au-ledger .au-snapshot-picker{justify-content:flex-end}
.au-ledger .au-snapshot-picker label{min-width:0;max-width:100%}
.au-ledger .au-timeline-entry>summary{cursor:pointer;padding:12px;display:grid;grid-template-columns:minmax(0,1fr) minmax(130px,.7fr) minmax(160px,1fr);gap:12px;align-items:center}
.au-ledger .au-timeline-session:before{content:'▸';display:inline-block;margin-right:8px;color:var(--ui-accent)}
.au-ledger .au-timeline-entry[open] .au-timeline-session:before{content:'▾'}
.au-ledger .au-timeline-entry dl{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;padding:12px;margin:0;border-top:1px solid var(--ui-stroke-secondary)}
.au-ledger .au-timeline-entry dt{font-size:.6875rem;color:var(--ui-text-tertiary);margin-bottom:5px}
.au-ledger .au-timeline-entry dd{margin:0;min-width:0}
@container(max-width:759px){.au-ledger .au-timeline-entry dl{grid-template-columns:repeat(2,minmax(0,1fr))}.au-ledger .au-timeline-entry>summary{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.au-ledger .au-timeline-session{grid-column:1/-1}}
@container(max-width:400px){.au-ledger .au-timeline-entry dl{grid-template-columns:minmax(0,1fr)}}
.au-ledger .au-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ui-accent);margin:0 8px 1px 0}
.au-ledger .au-chart{min-width:0}.au-ledger .au-chart svg{display:block;width:100%;min-height:160px;outline-offset:4px}
.au-ledger .au-chart:focus{outline:none}
.au-ledger .au-plot-hit{fill:transparent;stroke:transparent;stroke-width:1;vector-effect:non-scaling-stroke;cursor:crosshair;touch-action:none}
.au-ledger .au-plot-hit:hover{stroke:color-mix(in srgb,var(--ui-text-tertiary) 70%,var(--au-surface-bg))}
.au-ledger .au-chart:focus .au-plot-hit{stroke:color-mix(in srgb,var(--ui-accent) 55%,var(--au-surface-bg))}
.au-ledger .au-plot-selection{fill:#a799ef;fill-opacity:.3;pointer-events:none}
.au-ledger .au-chart-title{display:flex;justify-content:space-between;gap:12px;font-size:.875rem;margin:5px 0 10px}
.au-ledger .au-axis{font-size:.6875rem;fill:var(--ui-text-tertiary)}.au-ledger .au-gridline{stroke:var(--ui-stroke-secondary);stroke-width:1}
.au-ledger .au-line{fill:none;stroke:var(--ui-accent);stroke-width:2;stroke-linejoin:round;vector-effect:non-scaling-stroke}.au-ledger .au-area{fill:var(--ui-accent);opacity:.10}
.au-ledger .au-crosshair{stroke:var(--ui-text-tertiary);stroke-dasharray:4 4}.au-ledger .au-point{fill:var(--ui-accent);stroke:var(--au-surface-bg);stroke-width:2}
.au-ledger .au-chart-tip{min-height:26px;font-size:.6875rem;color:var(--ui-text-secondary);text-align:right;padding-right:10px}
.au-ledger .au-totals{gap:12px;grid-template-columns:repeat(7,minmax(0,1fr));margin:10px 0 20px}
.au-ledger .au-totals .au-metric{border:1px solid var(--ui-stroke-secondary);border-radius:6px;padding:10px 12px}
.au-ledger .au-totals .au-number{font-size:1.5625rem;font-weight:580;margin:7px 0}
.au-ledger .au-totals .au-metric>.au-muted:last-child{font-size:.625rem;line-height:1.4}
.au-ledger .au-view-controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:16px 0 9px}
.au-ledger .au-segment{display:inline-flex;gap:3px;align-items:center;border:1px solid var(--ui-stroke-secondary);background:transparent;padding:3px;border-radius:9px;max-width:100%}
.au-ledger .au-segment button{background:transparent;border:1px solid transparent;padding:6px 11px;color:var(--ui-text-tertiary);border-radius:6px;white-space:nowrap}
/* All selectable controls share one unmistakable selected state. The same rules
   run in Desktop and the preview; selection is never just a transient focus ring. */
.au-ledger .au-tabs button,.au-ledger .au-segment button{font-weight:600}
.au-ledger button[aria-selected="true"],
.au-ledger button[aria-pressed="true"],
.au-ledger button[aria-checked="true"]{
 background-color:var(--au-selected-bg);border-color:var(--ui-accent,#a799ef);
 color:var(--ui-accent,#a799ef);box-shadow:inset 0 -2px 0 var(--ui-accent,#a799ef)
}
.au-ledger button:hover:not(:disabled):not([aria-selected="true"]):not([aria-pressed="true"]):not([aria-checked="true"]){background-color:var(--au-hover-bg)}
.au-ledger button:focus-visible,.au-ledger select:focus-visible,.au-ledger input:focus-visible,.au-ledger summary:focus-visible{outline:2px solid var(--ui-accent,#a799ef);outline-offset:3px}
.au-ledger .au-provider-select,.au-ledger .au-mode-select,.au-ledger .au-period-select{border-color:var(--ui-accent,#a799ef)}
.au-ledger .au-section-summary{margin:12px 0 18px}
.au-ledger .au-window-label{margin-left:auto;font-size:.6875rem}.au-ledger .au-mode-select,.au-ledger .au-period-select{display:none}
.au-ledger .au-filters{opacity:.94}.au-ledger .au-filters button,.au-ledger .au-filters input,.au-ledger .au-filters select{padding:5px 9px;font-size:.75rem}
/* Use a small flex basis so native select option widths cannot force a
   second toolbar row before the available space has been shared. */
.au-ledger .au-filters select{flex:1 1 105px;min-width:95px;max-width:180px;overflow:hidden;text-overflow:ellipsis}
.au-ledger .au-filters select[aria-label="Saved tests"]{flex-basis:145px;min-width:130px;max-width:247px}
/* Keep the controls on one row through the three-column metric layout.
   Allow their ordinary wrapping only after the metrics switch to two columns. */
@container (min-width:621px) and (max-width:980px){
 .au-ledger .au-filters{gap:2px}
 .au-ledger .au-filters select{flex:1 1 95px;min-width:90px;padding-inline:4px}
 .au-ledger .au-filters select[aria-label="Project"]{flex-basis:98px;min-width:98px}
 .au-ledger .au-filters select[aria-label="Saved tests"]{flex-basis:108px;min-width:108px}
 .au-ledger .au-filters input[aria-label="Session ID"]{flex:1 1 70px;min-width:70px;padding-inline:3px}
 .au-ledger .au-filters button{padding-inline:4px}
}
.au-ledger .au-quality-line{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.au-ledger .au-quality{margin:8px 0 0;color:var(--ui-text-tertiary);font-size:.6875rem}
.au-ledger details>summary{cursor:pointer}.au-ledger .au-quota-collapsible{margin-top:24px;padding:16px 0;border-top:1px solid var(--ui-stroke-secondary)}
.au-ledger .au-breakdown{margin-top:16px}.au-ledger .au-breakdown td:not(:first-child),.au-ledger .au-breakdown th:not(:first-child){text-align:right}
.au-ledger .au-breakdown th:first-child,.au-ledger .au-breakdown td:first-child{padding-left:0}
@container (max-width:980px){.au-ledger .au-totals{grid-template-columns:repeat(3,minmax(0,1fr))}.au-ledger .au-hero{gap:25px;grid-template-columns:minmax(170px,.7fr) minmax(310px,1.5fr)}.au-ledger .au-window-label{flex-basis:100%;margin-left:0}.au-ledger .au-period-buttons{display:none}.au-ledger .au-period-select{display:block}}
@container (max-width:620px){.au-ledger .au-hero{display:block}.au-ledger .au-big{font-size:2.4375rem}.au-ledger .au-provider-totals{margin-top:18px}.au-ledger .au-chart{margin-top:28px}.au-ledger .au-totals{grid-template-columns:repeat(2,minmax(0,1fr))}.au-ledger .au-mode-buttons{display:none}.au-ledger .au-mode-select{display:block}.au-ledger .au-chart-title{flex-wrap:wrap}.au-ledger .au-totals .au-number{font-size:1.4375rem}.au-ledger .au-quality-line{display:block}}

.au-ledger .au-subagent-card{display:flex;flex-direction:column;align-items:flex-start;text-align:left;border:1px solid var(--ui-stroke-secondary);padding:10px 12px;min-width:0;background:transparent}
.au-ledger .au-subagent-card .au-muted:last-child{font-size:.625rem}
.au-ledger .au-main-tabs{margin-top:18px}.au-ledger .au-quota-home{margin-top:10px}
.au-ledger .au-subpage-tabs{margin-top:16px}.au-ledger .au-subpage-tabs button{font-size:.75rem}
.au-ledger .au-provider-select{margin:14px 0;width:100%}
.au-ledger .au-quota-home hr{border:0;border-top:1px solid var(--ui-stroke-secondary);margin:12px 0}
.au-ledger .au-quota-home [class~='w-28']{width:112px;flex-shrink:0}
.au-ledger .au-quota-home .border{background:transparent;border-radius:5px;padding:12px;margin:0}
.au-ledger .au-quota-home .border button{background:transparent;border:0;padding:0 4px}
.au-ledger .au-quota-home button.au-provider-name{padding:2px 4px;margin:-2px -4px;border:0;background:transparent;color:var(--ui-text-primary);text-align:left;white-space:normal;overflow-wrap:anywhere}
.au-ledger .au-quota-home button.au-provider-name:is(:hover,:focus-visible){color:var(--ui-accent);background:var(--au-selected-bg);text-decoration:underline;text-underline-offset:3px}
.au-ledger .au-provider-limits{margin:0 0 16px;min-width:0}
.au-quota-card{container-type:inline-size;min-width:0}
.au-quota-card>div:first-child{flex-wrap:wrap}
.au-reset-header{position:relative;display:flex;align-items:center;gap:8px;font-size:.6875rem;flex-shrink:0}
.au-reset-auto{white-space:nowrap;cursor:pointer;color:var(--ui-text-secondary)}
.au-reset-tooltip{position:absolute;top:calc(100% + 6px);right:0;z-index:21;width:min(310px,80vw);padding:8px 10px;border:1px solid var(--ui-stroke-secondary);border-radius:5px;background:var(--au-surface-bg);color:var(--ui-text-secondary);white-space:normal;line-height:1.5;overflow-wrap:anywhere;box-shadow:0 8px 25px color-mix(in srgb,var(--ui-text-primary) 18%,transparent);visibility:hidden;pointer-events:none}
.au-reset-auto:is(:hover,:focus-within) .au-reset-tooltip{visibility:visible}
.au-reset-auto input{vertical-align:middle;accent-color:var(--ui-accent)}
.au-reset-auto:has(input:disabled){opacity:.6;cursor:not-allowed}
.au-reset-badge{padding:3px 7px!important;border:1px solid var(--ui-stroke-secondary)!important;border-radius:12px!important;white-space:nowrap;font-weight:600}
.au-reset-badge[data-tone="zero"]{color:var(--ui-text-quaternary)}
.au-reset-badge[data-tone="waiting"]{color:var(--ui-text-warning,#d2b776);border-color:var(--ui-text-warning,#d2b776)!important}
.au-reset-badge[data-tone="ready"]{color:var(--ui-text-success,#64bba8);border-color:var(--ui-text-success,#64bba8)!important}
.au-reset-popover{position:absolute;z-index:20;top:calc(100% + 6px);right:0;width:min(320px,80vw);padding:12px;border:1px solid var(--ui-stroke-secondary);border-radius:5px;background:var(--au-surface-bg);color:var(--ui-text-primary);box-shadow:0 8px 25px color-mix(in srgb,var(--ui-text-primary) 18%,transparent);white-space:normal;line-height:1.45}
.au-reset-popover button{margin:6px 6px 0 0!important;padding:4px 8px!important;border:1px solid var(--ui-stroke-secondary)!important;border-radius:4px!important}
.au-reset-popover button:disabled{cursor:not-allowed;opacity:.65}
.au-reset-popover .au-reset-blocked{color:var(--ui-text-error,#d58c96)!important;border-color:var(--ui-stroke-error,#613f49)!important}
.au-reset-reason{color:var(--ui-text-secondary);margin-top:5px}
.au-quota-rows{display:grid;grid-template-columns:minmax(0,1fr);gap:8px 24px}
.au-quota-rows>*{min-width:0;overflow-wrap:anywhere}
.au-quota-rows:empty{display:none}
@container (min-width:520px){.au-quota-rows[data-columns="multiple"]{grid-template-columns:repeat(2,minmax(0,1fr))}}
.au-ledger .au-provider-limits .border button{background:transparent;border:0;padding:0 4px}
.au-ledger .au-provider-quota-status{display:flex;align-items:center;gap:10px;min-height:44px;color:var(--ui-text-tertiary)}
.au-ledger .au-drill{background:transparent;border:0;text-align:left;padding:0;color:var(--ui-accent);max-width:260px;white-space:normal;overflow-wrap:anywhere}
.au-ledger .au-drill small{display:block;color:var(--ui-text-tertiary);font-size:.625rem}
.au-ledger .au-request-navigation{display:flex;align-items:center;flex-wrap:wrap;gap:10px 14px;margin:12px 0 16px;padding:0;min-width:0}
.au-ledger .au-return-actions{display:flex;align-items:center;justify-content:flex-end;gap:18px;flex-wrap:nowrap;flex:none;margin-left:auto}
.au-ledger .au-return-button{display:inline-flex;align-items:center;justify-content:center;gap:6px;font-size:.75rem;line-height:16px;min-height:30px;padding:7px 0;border:0;border-radius:0;background:transparent;box-shadow:none;color:var(--ui-accent);font-weight:600;white-space:nowrap}
.au-ledger button.au-return-button:hover:not(:disabled):not([aria-selected="true"]):not([aria-pressed="true"]):not([aria-checked="true"]){background:transparent;text-decoration:underline;text-underline-offset:3px}
.au-ledger .au-return-button .au-back-icon{display:block;flex:none;width:14px;height:14px}
.au-ledger .au-return-button .au-return-label{display:block;line-height:16px}
.au-ledger .au-active-scopes{display:flex;align-items:center;gap:6px;flex-wrap:wrap;min-width:0}
.au-ledger .au-scope-chip{display:inline-flex;align-items:center;gap:8px;max-width:100%;min-width:0;padding:5px 9px;font-size:.6875rem;border-radius:999px;background:var(--au-control-bg);color:var(--ui-text-secondary)}
.au-ledger .au-scope-chip>span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:300px}
.au-ledger .au-scope-chip>span:last-child{flex:none}
@container(max-width:620px){.au-ledger .au-active-scopes{flex-basis:100%}.au-ledger .au-request-navigation{gap:8px}.au-ledger .au-scope-chip>span:first-child{max-width:230px}}
.au-ledger .au-breakdown-options{flex-wrap:wrap}
@container (max-width:900px){.au-ledger .au-totals{grid-template-columns:repeat(3,minmax(0,1fr))}}
@container (max-width:620px){.au-ledger .au-main-tabs{flex-wrap:nowrap;overflow-x:auto}.au-ledger .au-main-tabs button{flex-shrink:0}.au-ledger .au-totals{grid-template-columns:repeat(2,minmax(0,1fr))}}

.au-ledger .au-field-label{display:none}
.au-ledger .au-skill-chart{display:grid;grid-template-columns:minmax(0,1fr);gap:24px;align-items:start}
.au-ledger .au-skill-pie{display:block;width:100%;max-width:320px;height:auto;justify-self:center}
.au-ledger .au-skill-legend{display:grid;gap:6px;min-width:0}
@container(min-width:620px){.au-ledger .au-skill-legend{grid-template-columns:repeat(2,minmax(0,1fr))}}
@container(min-width:1000px){.au-ledger .au-skill-legend{grid-template-columns:repeat(3,minmax(0,1fr))}}
.au-ledger .au-skill-legend-row{display:flex;align-items:center;gap:9px;padding:8px;text-align:left;min-width:0;flex-wrap:wrap}
.au-ledger .au-skill-legend-row>span:not(.au-skill-swatch){flex:1;min-width:0;overflow-wrap:anywhere}
.au-ledger .au-skill-legend-row>strong{font-size:.75rem;font-variant-numeric:tabular-nums}
.au-ledger .au-skill-swatch{display:block;width:10px;height:10px;flex:none;border-radius:2px}
.au-ledger [data-testid="skills-usage"] .au-segment{flex-wrap:wrap}
.au-ledger [data-testid="skills-usage"] :is(h3,label){overflow-wrap:anywhere;min-width:0;max-width:100%}
@container(max-width:620px){.au-ledger .au-skill-chart{grid-template-columns:minmax(0,1fr);gap:14px}}
.au-ledger .au-table[data-layout="records"]{max-height:none;overflow:visible;background:transparent}
.au-ledger .au-table[data-layout="records"] table{display:block;white-space:normal}
.au-ledger .au-table[data-layout="records"] thead{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
.au-ledger .au-table[data-layout="records"] tbody,.au-ledger .au-table[data-layout="records"] tfoot{display:block}
.au-ledger .au-table[data-layout="records"] tr{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 20px;padding:14px;margin-bottom:10px;border:1px solid var(--ui-stroke-secondary);border-radius:7px;background:var(--au-table-bg)}
.au-ledger .au-table[data-layout="records"] thead tr{display:table-row;padding:0;margin:0}
.au-ledger .au-table[data-layout="records"] :is(td,th){display:block;min-width:0;padding:0;border:0;text-align:left;background:transparent;overflow-wrap:anywhere;white-space:normal}
.au-ledger .au-table[data-layout="records"] .au-field-label{display:block;color:var(--ui-text-tertiary);font-size:.6875rem;font-weight:400;margin-bottom:4px}
.au-ledger .au-table[data-layout="records"] .au-field-value{min-width:0;line-height:1.5;overflow-wrap:anywhere}
.au-ledger .au-table[data-layout="records"] .au-record-heading{grid-column:1 / -1;padding-bottom:10px;border-bottom:1px solid var(--ui-stroke-secondary);font-weight:600}
.au-ledger .au-table[data-layout="records"] .au-record-details{grid-column:1 / -1}
.au-ledger .au-table[data-layout="records"] .au-record-empty{display:none}
.au-ledger .au-table[data-layout="records"] .au-json-details{min-width:0}
.au-ledger .au-table[data-layout="records"] .au-json-panel{width:100%;max-width:100%;min-width:0}
.au-ledger .au-table[data-layout="records"] .au-drill{max-width:100%}
.au-ledger .au-record-disclosure{display:none}
.au-ledger .au-table[data-layout="records"][data-accordions="true"] .au-record-disclosure{display:flex;align-items:center;flex-wrap:wrap;gap:8px 16px;width:100%;border:0;background:transparent;text-align:left;padding:0;color:var(--ui-text-primary)}
.au-record-identity{flex:1;min-width:100px;overflow-wrap:anywhere}
.au-ledger .au-record-summary{display:inline-flex;flex-direction:column;min-width:0;overflow-wrap:anywhere}
.au-ledger .au-record-summary-label{color:var(--ui-text-tertiary);font-size:.6875rem;font-weight:400}
.au-ledger .au-table[data-layout="records"][data-accordions="true"] tbody .au-record-heading>.au-field-label,
.au-ledger .au-table[data-layout="records"][data-accordions="true"] tbody .au-record-heading>.au-field-value{display:none}
.au-ledger .au-table[data-layout="records"][data-accordions="true"] tbody tr[data-expanded="false"]>td:not(:first-child){display:none}
.au-ledger .au-table[data-layout="records"][data-accordions="true"] tbody tr[data-expanded="false"] .au-record-heading{padding-bottom:0;border-bottom:0}
@container(min-width:760px){.au-ledger .au-table[data-layout="records"] tr{grid-template-columns:repeat(3,minmax(0,1fr))}}
@container(max-width:400px){.au-ledger .au-table[data-layout="records"] tr{grid-template-columns:minmax(0,1fr)}}
/* The route tile already supplies a definite height. Do not put this flex
   allocation inside Radix's intrinsic-height scroll content wrapper. */
.au-ledger.au-pane{height:100%;min-height:0;box-sizing:border-box;overflow:hidden;container-type:size}
.au-provider-pane{height:100%;min-height:0;display:flex;flex-direction:column}
.au-upper{flex:0 1 auto;min-height:0;max-height:var(--au-upper-cap,50%);overflow:auto;scrollbar-gutter:stable;overflow-anchor:none}
.au-totals .au-metric>.au-muted:last-child{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:1.5em;line-height:1.5}
.au-ledger .au-provider-pane>.au-subpage-tabs{flex:none;flex-wrap:nowrap;overflow:auto;margin:0;min-height:0;max-height:25%;padding:8px 0}
.au-provider-pane>.au-subpage-tabs button{flex-shrink:0}
.au-reader{flex:1;min-height:0;overflow:auto;overflow-anchor:none;scrollbar-gutter:stable;position:relative}
.au-ledger .au-reader .au-table,.au-ledger .au-reader .au-table[data-layout="records"][data-accordions="true"],.au-ledger .au-reader .au-timeline-scroll{max-height:none;overflow:visible}
.au-ledger .au-reader thead{position:sticky;top:0;z-index:2;background:var(--au-table-bg)}
.au-reader-content{display:flow-root;min-width:0}
/* Synchronous normal-line measurement; never close live details or clone IDs. */
.au-reader.au-measure-lines .au-json-panel{display:none}
.au-reader.au-measure-sparse .au-table[data-layout="records"][data-accordions="true"] tr>td:not(:first-child),
.au-reader.au-measure-sparse .au-timeline-entry>:not(summary){display:none}
.au-reader.au-measure-sparse .au-table[data-layout="records"][data-accordions="true"] .au-record-heading{padding-bottom:0;border-bottom:0}
.au-pane .au-chart svg{height:clamp(160px,24cqh,320px)}
/* Interpolate compact spacing into the original tall layout (1400px).
   Container units track the pane, not the browser window or monitor. */
.au-pane .au-hero{margin:clamp(6px,calc(-26.667px + 3.333cqh),20px) 0 clamp(4px,calc(-35.667px + 4.333cqh),25px);gap:clamp(24px,calc(-64px + 8cqh),48px)}
.au-pane .au-big{font-size:clamp(2.25rem,calc(1.0909375rem + 2.182cqh),3rem);margin:clamp(2px,calc(-2px + .4cqh),4px) 0 clamp(4px,calc(-4px + .8cqh),8px)}
.au-pane .au-provider-totals{margin-top:clamp(10px,calc(-56px + 6cqh),28px)}
.au-pane .au-provider-row{margin:clamp(8px,calc(-25px + 3cqh),17px) 0;gap:clamp(4px,calc(-10.667px + 1.333cqh),8px)}
.au-pane .au-totals{gap:clamp(8px,calc(-6.667px + 1.333cqh),12px);margin:clamp(8px,calc(.667px + .667cqh),10px) 0 clamp(10px,calc(-26.667px + 3.333cqh),20px)}
.au-pane .au-totals .au-metric{padding:clamp(8px,calc(.667px + .667cqh),10px) clamp(8px,calc(-6.667px + 1.333cqh),12px)}
.au-pane .au-totals .au-number{margin:clamp(4px,calc(-7px + 1cqh),7px) 0}
.au-pane .au-view-controls{margin:clamp(8px,calc(-21.333px + 2.667cqh),16px) 0 clamp(8px,calc(4.333px + .333cqh),9px);gap:clamp(8px,calc(-6.667px + 1.333cqh),12px)}
.au-pane .au-chart svg{height:clamp(180px,calc(-333.333px + 46.667cqh),320px)}
.au-pane .au-chart-title{margin:clamp(2px,calc(-9px + 1cqh),5px) 0 clamp(4px,calc(-8.667px + 1.333cqh),10px)}
.au-pane .au-usage-summary{padding-bottom:clamp(6px,calc(-14px + 2cqh),14px)}
@container(max-height:1100px){.au-pane .au-chart svg{height:clamp(160px,calc(92px + 8cqh),180px)}}
/* The wide two-column summary is structural: toggling it at a height
   breakpoint made the summary grow as the pane gained a single pixel. */
@container(min-width:1100px){
 .au-pane .au-hero{grid-template-columns:minmax(0,1fr) minmax(360px,1.4fr)}
 .au-pane .au-provider-totals{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 16px}
 .au-pane .au-provider-row{margin:2px 0;overflow-wrap:anywhere}
}
@container(max-width:980px){.au-pane .au-hero{gap:clamp(24px,calc(20.333px + .333cqh),25px)}}
@container(max-width:620px){
 .au-pane .au-big{font-size:clamp(2.25rem,calc(1.96025rem + .545cqh),2.4375rem)}
 .au-pane .au-provider-totals{margin-top:clamp(10px,calc(-19.333px + 2.667cqh),18px)}
}
.au-reader:focus-visible,.au-upper:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
`
const bucketNames = {input_tokens:'Uncached input',output_tokens:'Output',cache_read_tokens:'Cache reads',cache_write_tokens:'Cache writes'}
const count = n => n == null ? '—' : Number(n).toLocaleString(undefined,{maximumFractionDigits:0})
const short = n => n == null ? '—' : Number(n).toLocaleString(undefined,{notation:'compact',maximumFractionDigits:2})
const money = n => n == null ? '—' : '$'+Number(n).toLocaleString(undefined,{minimumFractionDigits:4,maximumFractionDigits:6})
const when = t => t ? new Date(t*1000).toLocaleString() : '—'
const compactWhen = t => t ? new Date(t*1000).toLocaleTimeString() : '—'
function notice(t){return h('div',{className:'au-notice'},t)}
function metric(label,value,note,title){return h('div',{className:'au-metric',title:title||''},h('div',{className:'au-muted'},label),h('div',{className:'au-number'},value),h('div',{className:'au-muted'},note))}
// A bounded reader keeps a keyed visible entry, not a pixel count. Prepending
// records (or changing their heights) must not move the entry being read.
function readerSnapshot(root){
 const top=root.getBoundingClientRect().top;
 return {top:root.scrollTop,items:[...root.querySelectorAll('[data-scroll-key]')]
  .filter(node=>node.getBoundingClientRect().bottom>top&&node.getBoundingClientRect().top<top+root.clientHeight)
  .map(node=>({key:node.dataset.scrollKey,offset:node.getBoundingClientRect().top-top}))};
}
function restoreReader(root,snapshot){
 if(!snapshot)return;
 if(snapshot.top===0){root.scrollTop=0;return}
 const nodes=[...root.querySelectorAll('[data-scroll-key]')];
 for(const item of snapshot.items){const node=nodes.find(node=>node.dataset.scrollKey===item.key);
  if(node){root.scrollTop+=node.getBoundingClientRect().top-root.getBoundingClientRect().top-item.offset;return}}
 root.scrollTop=snapshot.top;
}
function Reader({children,className='',label,id,resetKey,...props}){
 const [state]=useState(()=>({root:null,snapshot:null}));
 if(!state.attach)state.attach=node=>{state.root=node};
 useEffect(()=>{
  const root=state.root;if(!root)return;
  const remember=()=>{state.snapshot=readerSnapshot(root)};
  const reconcile=()=>{restoreReader(root,state.snapshot);remember()};
  const observer=new MutationObserver(reconcile),resize=new ResizeObserver(reconcile);
  remember();root.addEventListener('scroll',remember,{passive:true});
  observer.observe(root,{childList:true,subtree:true,characterData:true});resize.observe(root.firstElementChild);
  return()=>{observer.disconnect();resize.disconnect();root.removeEventListener('scroll',remember)};
 },[]);
 useEffect(()=>{if(state.root){state.root.scrollTop=0;state.snapshot=readerSnapshot(state.root)}},[resetKey]);
 return h('div',{...props,id,ref:state.attach,className:'au-reader '+className,tabIndex:0,'aria-label':label},h('div',{className:'au-reader-content'},children));
}
function AnalyticsPane({children}){
 const [state]=useState(()=>({root:null}));
 if(!state.attach)state.attach=node=>{state.root=node};
 useEffect(()=>{
  const root=state.root;let frame=0,disposed=false;
  function measure(){
   frame=0;if(disposed)return;
   const pane=root.querySelector('.au-provider-pane'),reader=root.querySelector('.au-reader'),nav=root.querySelector('.au-subpage-tabs');
   if(!pane||!reader||!nav)return;
   // Reserve at most ten normal lines, independently of the current viewport.
   // Sparse tabs return unused space even beyond the usual two-thirds cap.
   // Expanded bodies never redefine a normal collapsed line.
   const rows=[...reader.querySelectorAll(reader.querySelector('tbody')?'tbody tr':reader.querySelector('.au-timeline-entry')?'.au-timeline-entry':'.au-skill-legend-row')];
   const scrollTop=reader.scrollTop;
   let heights,sparseNeed;
   // Measure even an initially open, sole record without its inspector body.
   // Restore in the same frame: no toggle events, lost state or scroll clamping.
   reader.classList.add('au-measure-lines');
   try{heights=rows.map(row=>{
    const style=getComputedStyle(row),disclosure=row.querySelector('.au-record-disclosure');
    const padding=parseFloat(style.paddingTop)+parseFloat(style.paddingBottom)+parseFloat(style.borderTopWidth)+parseFloat(style.borderBottomWidth);
    const margin=parseFloat(style.marginBottom)||0;
    if(row.matches('.au-timeline-entry'))return row.querySelector('summary').getBoundingClientRect().height+padding+margin;
    if(disclosure&&getComputedStyle(disclosure).display!=='none')return disclosure.getBoundingClientRect().height+padding+margin;
    return row.getBoundingClientRect().height+margin;
   }).filter(n=>n>0);
    if(rows.length<10){
     reader.classList.add('au-measure-sparse');
     sparseNeed=reader.firstElementChild.getBoundingClientRect().height+nav.getBoundingClientRect().height;
    }
   }finally{
    reader.classList.remove('au-measure-lines','au-measure-sparse');reader.scrollTop=scrollTop;
   }
   const line=heights.length?Math.max(...heights):0;
   const first=rows[0],last=rows[rows.length-1];
   const span=first&&last?last.getBoundingClientRect().bottom-first.getBoundingClientRect().top:0;
   const chrome=Math.max(0,reader.firstElementChild.getBoundingClientRect().height-span)+nav.getBoundingClientRect().height;
   const height=pane.clientHeight;
   let cap=sparseNeed!==undefined?Math.max(height/2,height-sparseNeed)
    :Math.max(height/2,Math.min(height*2/3,height-chrome-10*line));
   // Prioritise the chart on short panes, then taper back to the original
   // ten-row allocation as height grows. Do not jump at 600 or 1100px.
   // This remains a maximum: sparse upper content never gains blank filler.
   if(height>500&&height<1400){
    const reserve=Math.max(nav.getBoundingClientRect().height+120,Math.min(height*.45,chrome+2*line));
    const shortCap=Math.max(cap,height-reserve);
    const weight=Math.min(1,(height-500)/100,(1400-height)/300);
    cap+=weight*(shortCap-cap);
   }
   const rounded=Math.floor(cap);
   // Flooring a fractional cap can leave only the summary's bottom border
   // outside the scrollport. Fit that measured final pixel, but do not
   // enlarge genuinely scrolling content or take the reader's last row.
   const upper=pane.querySelector('.au-upper');
   const bottom=upper.lastElementChild?.getBoundingClientRect().bottom-upper.getBoundingClientRect().top+upper.scrollTop;
   const value=(bottom>rounded&&bottom<=rounded+1&&reader.clientHeight>120?Math.ceil(bottom):rounded)+'px';
   if(pane.style.getPropertyValue('--au-upper-cap')!==value)pane.style.setProperty('--au-upper-cap',value);
  }
  const schedule=()=>{if(!frame)frame=requestAnimationFrame(measure)};
  const resize=new ResizeObserver(schedule);resize.observe(root);
  const content=root.querySelector('.au-reader-content');if(content)resize.observe(content);
  const mutations=new MutationObserver(schedule);mutations.observe(root,{subtree:true,childList:true,characterData:true});
  root.addEventListener('toggle',schedule,true);root.addEventListener('au-reflow',schedule);schedule();
  return()=>{disposed=true;resize.disconnect();mutations.disconnect();root.removeEventListener('toggle',schedule,true);root.removeEventListener('au-reflow',schedule);cancelAnimationFrame(frame)};
 },[]);
 return h('div',{ref:state.attach,className:'au-ledger au-pane p-4'},children);
}
function ResponsiveTable({headers,rows,footer,rowKeys,accordions}){
 const [expanded,setExpanded]=useState({});
 const [nodes]=useState(()=>({root:null}));
 useEffect(()=>{
  const root=nodes.root;if(!root)return;
  let frame=0,lastWidth=-1,disposed=false;
  function measure(){
   frame=0;if(disposed)return;
   const reader=root.closest('.au-reader'),snapshot=reader?readerSnapshot(reader):null;
   const top=root.scrollTop,focused=document.activeElement;
   const focusedRow=focused?.closest('tbody tr');
   if(focusedRow&&root.contains(focusedRow)&&!focused.classList.contains('au-record-disclosure')){
    const key=focusedRow.dataset.rowId;
    if(key!=null)setExpanded(old=>old[key]?old:{...old,[key]:true});
   }
   // Measure the real table, not the stacked layout. Keep the same keyed nodes,
   // so resizing cannot discard an open JSON snapshot or keyboard focus.
   root.dataset.layout='table';
   root.dataset.layout=root.querySelector('table').scrollWidth>root.clientWidth+1?'records':'table';
   if(root.dataset.layout==='records'&&focusedRow&&root.contains(focused)&&
      (focused===focusedRow||focused.closest('td')===focusedRow.querySelector('td:first-child'))&&
      !focused.classList.contains('au-record-disclosure')){
    focusedRow.querySelector('.au-record-disclosure')?.focus({preventScroll:true});
   }
   if(root.dataset.layout==='table'&&root.contains(focused)&&focused.classList.contains('au-record-disclosure')){
    focusedRow.tabIndex=-1;focusedRow.focus({preventScroll:true});
    focusedRow.addEventListener('blur',()=>focusedRow.removeAttribute('tabindex'),{once:true});
   }
   root.scrollTop=top;if(reader)restoreReader(reader,snapshot);
   root.dispatchEvent(new Event('au-reflow',{bubbles:true}));
  }
  function schedule(){if(!frame)frame=requestAnimationFrame(measure)}
  const observer=new ResizeObserver(entries=>{
   const width=entries[0].contentRect.width;
   if(width!==lastWidth){lastWidth=width;schedule()}
  });
  observer.observe(root);root.addEventListener('toggle',schedule,true);schedule();
  document.fonts?.ready.then(()=>{if(!disposed)schedule()});
  return()=>{disposed=true;observer.disconnect();root.removeEventListener('toggle',schedule,true);cancelAnimationFrame(frame)};
 },[headers,rows,footer]);
 const cells=(row,total=false,entry=null,key=null)=>row.map((value,i)=>h(total&&i===0?'th':'td',{
  key:i,role:total&&i===0?'rowheader':'cell',...(total&&i===0?{scope:'row'}:{}),
  className:(i===0?'au-record-heading ':headers[i]==='Details'?'au-record-details ':'')+(value===''?'au-record-empty':'')
 },i===0&&entry?h('button',{type:'button',className:'au-record-disclosure','aria-expanded':!!expanded[key],onClick:()=>setExpanded(old=>({...old,[key]:!old[key]}))},
  h('span',{'aria-hidden':true},expanded[key]?'▾':'▸'),h('span',{className:'au-record-identity'},entry.identity),
  entry.started?h('span',{className:'au-muted'},when(entry.started)):null,
  ...(entry.summary||[]).map((item,j)=>h('span',{key:j,className:'au-record-summary',title:item.title},
   h('span',{className:'au-record-summary-label'},item.label),h('strong',{},item.value))),
  entry.value!=null?h('strong',{},entry.value):null):null,
 h('span',{className:'au-field-label','aria-hidden':true},headers[i]),h('div',{className:'au-field-value'},value)));
 return h('div',{className:'au-table','data-accordions':!!accordions,tabIndex:accordions?0:undefined,'aria-label':accordions?'Expandable records':undefined,ref:node=>{nodes.root=node}},h('table',{role:'table'},
  h('thead',{role:'rowgroup'},h('tr',{role:'row'},...headers.map((v,i)=>h('th',{key:i,scope:'col',role:'columnheader'},v)))),
  h('tbody',{role:'rowgroup'},...rows.map((row,i)=>{const key=rowKeys?.[i]??i;return h('tr',{role:'row',key,'data-row-id':rowKeys?.[i],'data-scroll-key':'row:'+key,'data-expanded':!!expanded[key]},...cells(row,false,accordions?.[i],key))})),
  footer?h('tfoot',{role:'rowgroup'},h('tr',{role:'row'},...cells(footer,true))):null));
}
function table(headers,rows,footer=null,rowKeys=null,accordions=null){return h(ResponsiveTable,{headers,rows,footer,rowKeys,accordions})}
function downloadFile(name,body,type='text/csv'){const a=document.createElement('a'),url=URL.createObjectURL(new Blob([body],{type}));a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
function csvText(rows){if(!rows.length)return '';const keys=[...new Set(rows.flatMap(row=>Object.keys(row)))],cell=v=>'"'+String(typeof v==='object'&&v!==null?JSON.stringify(v):v??'').replace(/^[=+@-]/,"'$&").replaceAll('"','""')+'"';return [keys.map(cell).join(','),...rows.map(row=>keys.map(k=>cell(row[k])).join(','))].join('\r\n')}
// The SDK clipboard is available in Desktop even when Web Clipboard is denied.
// Every path is user-initiated; this never reads the clipboard.
async function writeJsonClipboard(text){
 try{if(typeof pluginOs?.writeClipboard==='function'&&await pluginOs.writeClipboard(text))return true}catch(_){}
 try{if(typeof navigator?.clipboard?.writeText==='function'){await navigator.clipboard.writeText(text);return true}}catch(_){}
 // Older shells/plain local-file previews: retain the user's existing selection.
 const focused=document.activeElement,selection=window.getSelection(),ranges=[];
 if(selection)for(let i=0;i<selection.rangeCount;i++)ranges.push(selection.getRangeAt(i).cloneRange());
 const area=document.createElement('textarea');area.value=text;area.readOnly=true;area.setAttribute('aria-label','JSON clipboard buffer');
 Object.assign(area.style,{position:'fixed',inset:'0 auto auto 0',width:'1px',height:'1px',opacity:'0',pointerEvents:'none',userSelect:'text'});
 document.body.appendChild(area);let ok=false;
 try{area.focus({preventScroll:true});area.select();ok=typeof document.execCommand==='function'&&document.execCommand('copy')}catch(_){}
 finally{area.remove();try{focused?.focus({preventScroll:true});selection?.removeAllRanges();ranges.forEach(r=>selection?.addRange(r))}catch(_){}}
 return ok;
}
function JsonRecord({record,entryKey,entries}){
 const [entry,setEntry]=useState(()=>entries.get(entryKey)||null),[copyState,setCopyState]=useState('idle');
 const [nodes]=useState(()=>({pre:null,timer:null,entry:null,attach:null}));
 nodes.entry=entry;
 // A stable ref avoids detaching/reattaching (and resetting scroll) on every poll.
 if(!nodes.attach)nodes.attach=node=>{
  if(nodes.pre&&nodes.entry){nodes.entry.top=nodes.pre.scrollTop;nodes.entry.left=nodes.pre.scrollLeft}
  nodes.pre=node;
  if(node&&nodes.entry){node.scrollTop=nodes.entry.top;node.scrollLeft=nodes.entry.left}
 };
 useEffect(()=>()=>{if(nodes.timer)clearTimeout(nodes.timer)},[]);
 function toggle(e){
  const open=e.currentTarget.open;
  if(open&&!entry){const next={json:JSON.stringify(record,null,2),top:0,left:0};entries.set(entryKey,next);setEntry(next)}
  else if(!open&&entry){entries.delete(entryKey);setEntry(null);setCopyState('idle')}
 }
 async function copy(e){
  e.preventDefault();e.stopPropagation();const text=entry?.json||JSON.stringify(record,null,2);
  const ok=await writeJsonClipboard(text);setCopyState(ok?'copied':'failed');
  if(nodes.timer)clearTimeout(nodes.timer);
  nodes.timer=setTimeout(()=>setCopyState('idle'),2500);
 }
 const icon=h('svg',{viewBox:'0 0 24 24',width:16,height:16,fill:'none',stroke:'currentColor',strokeWidth:1.7,strokeLinecap:'round',strokeLinejoin:'round','aria-hidden':true,focusable:false},
  copyState==='copied'?h('path',{d:'M5 12l4 4L19 6'}):[h('rect',{key:'a',x:8,y:8,width:12,height:12,rx:2}),h('path',{key:'b',d:'M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3'})]);
 return h('details',{className:'au-json-details',open:!!entry,onToggle:toggle,'data-testid':'usage-details','data-record-id':record.id},
  h('summary',{title:'Snapshot stays fixed while open. Close and reopen for newer values.'},'Usage / routing'),
  h('div',{className:'au-json-panel'},
   h('div',{className:'au-json-toolbar'},h('span',{className:'au-json-copy-status',role:'status','aria-live':'polite'},copyState==='copied'?'Copied':copyState==='failed'?'Copy unavailable — select text to copy':''),
    h('button',{type:'button',className:'au-json-copy',onClick:copy,'data-testid':'copy-json','aria-label':'Copy JSON to clipboard',title:copyState==='copied'?'Copied':'Copy full JSON'},icon)),
   h('pre',{className:'au-json-text',tabIndex:0,'aria-label':'Request or compression JSON',
    ref:nodes.attach,
    onScroll:e=>{if(entry){entry.top=e.currentTarget.scrollTop;entry.left=e.currentTarget.scrollLeft}}},
    h('code',{},entry?.json||JSON.stringify(record,null,2)))))
}
function tokensDetail(r,details){
 const key=JSON.stringify([details.scope,r.kind?'compression':'request',r.id]);
 return h(JsonRecord,{key,record:r,entryKey:key,entries:details.entries});
}

// Use the normalized exclusive-input bucket, not subtraction of partial aggregates.
// An all-unknown bucket is not zero; retain the missing-field indicators.
const SESSION_WRITE_BASIS='Calculated per session: max(0, current cache reads − previous cache reads).';
function sessionWrites(summary,format=short){
 if(!summary?.attempts)return format(0);
 const w=summary.session_cache_writes;
 return w?.compared_requests?format(w.tokens):'—';
}
function sessionWriteNote(summary){
 const w=summary?.session_cache_writes;
 return w?.compared_requests?'Calculated from session reads':w?.baseline_requests?'First read sets the baseline':'Awaiting read comparison';
}
function knownTokens(summary,key,format=short){
   if(key==='cache_write_tokens')return sessionWrites(summary,format);
  return summary?.attempts > (summary?.missing_fields?.[key]||0) ? format(summary.known?.[key]) : '—'
}

const dollars = n => n == null ? '—' : Number(n).toLocaleString(undefined,{style:'currency',currency:'USD',currencyDisplay:'narrowSymbol',minimumFractionDigits:2,maximumFractionDigits:2})
const uiNames = {'openai-codex':'Codex',nous:'Nous Portal',ollama:'Ollama Cloud','ollama-cloud':'Ollama Cloud',openrouter:'OpenRouter'}
function selectedCost(s){return !s?.attempts?'$0.00':Object.keys(bucketNames).some(k=>(s.cost_missing_fields?.[k]||0)<s.attempts)?dollars(s.known_cost_usd):'—'}
function savingsValue(s,key='cache_savings_usd'){return !s?.attempts?'$0.00':s.attempts>(s.savings_missing?.[key]??s.attempts)?dollars(s.savings?.[key]):'—'}
function viewTokens(s,key,format=short){return !s?.attempts?'0':knownTokens(s,key,format)}
function valueFor(s,mode){if(!s?.attempts)return 0;if(mode==='Cost')return Object.keys(bucketNames).some(k=>(s.cost_missing_fields?.[k]||0)<s.attempts)?Number(s.known_cost_usd):null;return s.attempts>(s.missing_fields?.total_tokens||0)?Number(s.known?.total_tokens):null}
function UsageChart({data,mode,onSelectRange}){
 const drag=useRef(null),chartRef=useRef(null),[selection,setSelection]=useState(null);
 useEffect(()=>{
  const blurOutside=e=>{const chart=chartRef.current;if(chart&&e.target.closest?.('.au-plot-hit')!==chart.querySelector('.au-plot-hit')){if(chart.contains(e.target))e.preventDefault();if(document.activeElement===chart)chart.blur()}};
  document.addEventListener('pointerdown',blurOutside,true);
  return()=>document.removeEventListener('pointerdown',blurOutside,true);
 },[]);
 useEffect(()=>{drag.current=null;setSelection(null);setHover(null)},[data?.window?.start,data?.window?.end,mode]);
 const [hover,setHover]=useState(null),points=data?.trend?.buckets||[],unit=data?.trend?.unit||'day',cost=mode==='Cost',vals=points.map(p=>valueFor(p,mode)),max=Math.max(1,...vals.filter(v=>v!=null)),top=max*1.15;
 const W=760,H=258,L=65,R=18,T=14,B=39,w=W-L-R,hg=H-T-B,plotStart=points[0]?.start||0,plotEnd=points[points.length-1]?.end||plotStart+1;
 const xx=i=>L+((points[i]?.start??plotStart)-plotStart)/Math.max(1,plotEnd-plotStart)*w,yy=v=>H-B-(v/top)*hg;
 const plotTime=x=>plotStart+(x-L)/w*(plotEnd-plotStart);
 let line='',area='',segment=[];function finish(){if(!segment.length)return;line+=segment.map((q,i)=>(i?' L ':' M ')+q[0]+','+q[1]).join('');area+=' M '+segment[0][0]+','+(H-B)+' L '+segment.map(q=>q.join(',')).join(' L ')+' L '+segment[segment.length-1][0]+','+(H-B)+' Z';segment=[]}
 vals.forEach((v,i)=>{if(v==null)finish();else segment.push([xx(i),yy(v)])});finish();
 const stamp=t=>new Date(t*1000).toLocaleString(undefined,(data?.trend?.seconds||86400)<86400?{hour:'numeric',minute:'2-digit',timeZone:'UTC'}:{month:'short',day:'numeric',timeZone:'UTC'}),active=hover==null?null:points[Math.min(hover,points.length-1)];
 // SVG screen transforms include preserveAspectRatio letterboxing in short panes.
 const plotX=e=>{const svg=e.currentTarget.ownerSVGElement,p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;return Math.max(L,Math.min(W-R,p.matrixTransform(svg.getScreenCTM().inverse()).x))};
 const move=e=>{const x=plotX(e),time=plotTime(x),index=points.findIndex(p=>p.end>time);setHover(index<0?points.length-1:index);const d=drag.current;if(d&&d.id===e.pointerId){d.x=x;d.moved=Math.abs(e.clientX-d.clientX)>=5;setSelection(d.moved?[d.start,x]:null)}};
 const cancel=()=>{drag.current=null;setSelection(null)};
 const release=e=>{const d=drag.current;if(!d||d.id!==e.pointerId)return;move(e);cancel();if(e.currentTarget.hasPointerCapture(e.pointerId))e.currentTarget.releasePointerCapture(e.pointerId);
  if(!d.moved||!onSelectRange||!points.length)return;
  const lo=Math.floor(plotTime(Math.min(d.start,d.x))),hi=Math.ceil(plotTime(Math.max(d.start,d.x)));
  if(hi>lo)onSelectRange(lo,hi);
 };
 return h('div',{ref:node=>{chartRef.current=node},className:'au-chart','data-testid':'usage-chart',tabIndex:0,role:'group','aria-label':'Usage chart. Arrow keys inspect time buckets.',onKeyDown:e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();setHover(Math.max(0,Math.min(points.length-1,(hover??0)+(e.key==='ArrowRight'?1:-1))))}}},
 h('div',{className:'au-chart-title'},(unit==='hour'?'Hourly':unit==='day'?'Daily':unit+' bucket')+(cost?' cost':' processed tokens'),h('span',{className:'au-muted'},'UTC · by request start')),
 h('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':(cost?'Cost':'Token')+' history, '+points.length+' UTC '+unit+' buckets'},
 ...[0,.25,.5,.75,1].map((f,i)=>h('g',{key:i},h('line',{x1:L,y1:yy(top*f),x2:W-R,y2:yy(top*f),className:'au-gridline'}),h('text',{x:L-10,y:yy(top*f)+4,textAnchor:'end',className:'au-axis'},cost?dollars(top*f):short(top*f)))),
 h('path',{d:area,className:'au-area'}),h('path',{d:line,className:'au-line'}),
 ...[0,Math.floor((points.length-1)/2),points.length-1].filter((n,i,a)=>n>=0&&n<points.length&&a.indexOf(n)===i).map(i=>h('text',{key:i,x:xx(i),y:H-10,textAnchor:i===0?'start':i===points.length-1?'end':'middle',className:'au-axis'},stamp(points[i].start))),
 active?h('line',{x1:xx(hover),x2:xx(hover),y1:T,y2:H-B,className:'au-crosshair'}):null,
 active&&vals[hover]!=null?h('circle',{cx:xx(hover),cy:yy(vals[hover]),r:4,className:'au-point'}):null,
 selection?h('rect',{key:'selection',className:'au-plot-selection',x:Math.min(...selection),y:T,width:Math.abs(selection[1]-selection[0]),height:hg}):null,
 h('rect',{key:'hit',className:'au-plot-hit',x:L,y:T,width:w,height:hg,
  onPointerDown:e=>{if(e.button!==0||!e.isPrimary||!points.length)return;e.preventDefault();e.currentTarget.closest('.au-chart').focus({preventScroll:true});const x=plotX(e);drag.current={id:e.pointerId,start:x,x,clientX:e.clientX,moved:false};e.currentTarget.setPointerCapture(e.pointerId)},
  onPointerMove:move,onPointerUp:release,onPointerCancel:cancel,onLostPointerCapture:cancel,onPointerLeave:()=>{if(!drag.current)setHover(null)}},
  h('title',{},'Click to interact with Left/Right arrow keys. Click and drag to zoom into a time range.'))),
 active?h('div',{className:'au-chart-tip',role:'status'},new Date(active.start*1000).toLocaleString(undefined,{timeZone:'UTC'})+' UTC · ',cost?selectedCost(active):viewTokens(active,'total_tokens',count)+' tokens',' · '+count(active.attempts)+' requests',active.unpriced_requests&&cost?' · partial price coverage':active.missing_usage?' · includes missing usage':''):h('div',{className:'au-chart-tip au-muted'},data?.summary?.attempts?'':'No recorded activity in this window.'),
 )
}
function missingFieldNote(summary,key){
 const n=summary?.missing_fields?.[key]||0;if(!n)return 'Reported tokens';
 const labels={awaiting_usage:'awaiting usage (owner process observed live)',unresolved_execution:'unresolved execution; usage unknown',abandoned_execution:'abandoned execution; field not reported',unverified_accounting:'unverified cache accounting',ended_without_usage:'ended without reported usage',unreported_field:'field not reported'};
 const reasons=summary?.missing_reasons?.[key];
 const parts=reasons?Object.entries(labels).filter(([name])=>reasons[name]>0).map(([name,label])=>count(reasons[name])+' '+label):[];
 return (parts.length?parts.join(' · '):count(n)+' requests without this value')+' · subtotal';
}
function Summary({data,label,mode,agent,onSubagents,onSelectRange}){
 const s=data?.summary,k=s?.known||{},missing=s?.missing_fields||{};
 if(!s)return h('section',{'data-testid':'recorded-summary',role:'status'},data?'Recorded usage unavailable for this selection.':'Loading recorded usage…');
 const t=key=>data?viewTokens(s,key):'—';
 const note=key=>{const awaiting=s.missing_reasons?.[key]?.awaiting_usage||0;
  return h('span',{title:missingFieldNote(s,key)},awaiting?count(awaiting)+' awaiting usage · subtotal':missing[key]?'Known subtotal':'Reported tokens')};
 const groups=data?.provider_groups||[];
 const providerRows=groups.map(g=>h('div',{key:g.provider,className:'au-provider-row'},
   h('span',{},h('i',{className:'au-dot'}),uiNames[g.provider]||g.provider,h('small',{},' '+count(g.sessions)+' sessions')),
   h('strong',{},mode==='Cost'?selectedCost(g):viewTokens(g,'total_tokens')),
   h('div',{className:'au-muted'},(k.total_tokens?((g.known.total_tokens/k.total_tokens)*100).toFixed(1):'0.0')+'% of known tokens · ',h('strong',{className:'au-provider-cost'},selectedCost(g)),g.unpriced_requests?' · partial':'')));
 return h('section',{className:'au-usage-summary','data-testid':'recorded-summary','aria-label':label+' usage totals'},
 h('div',{className:'au-metrics au-totals au-section-summary','data-testid':'usage-totals'},
 metric('Processed tokens',t('total_tokens'),note('total_tokens'),count(k.total_tokens)),
 metric('Cached input',t('cache_read_tokens'),s?.cache_hit_rate!=null?(100*s.cache_hit_rate).toFixed(2)+'% weighted cache hit':note('cache_read_tokens'),'Tokens served from cache; cache reads'),
 metric('Uncached input',t('input_tokens'),note('input_tokens'),'Input processed without cache reads or writes'),
 metric('Output',t('output_tokens'),note('output_tokens'),'Includes reasoning when reported'),
 metric('Cache writes',sessionWrites(s),sessionWriteNote(s),SESSION_WRITE_BASIS),
 metric('Cache savings',savingsValue(s),(s?.savings_missing?.cache_savings_usd?count(s.savings_missing.cache_savings_usd)+' incomplete savings estimates · subtotal':'Net of cache-write premium'),'Estimated read discount minus extra cache-write cost at the same rates. Incomplete estimates can reflect unavailable token fields, not just missing prices.'),
 h('button',{className:'au-subagent-card','data-testid':'subagent-summary','aria-pressed':agent==='subagent',onClick:onSubagents,title:'Filter to subagent requests. These tokens are already included in the total.'},
 h('span',{className:'au-muted'},'Subagent tokens'),h('span',{className:'au-number'},viewTokens(data?.subagent_summary,'total_tokens')),
 h('span',{className:'au-muted'},count(data?.subagent_summary?.agents||0)+' agents · '+count(data?.subagent_summary?.attempts||0)+' requests'),
 h('span',{className:'au-muted'},(k.total_tokens?100*(data?.subagent_summary?.known?.total_tokens||0)/k.total_tokens:0).toFixed(1)+'% of known tokens · '+selectedCost(data?.subagent_summary)))),
 mode!=='Limits'?h('div',{className:'au-hero','data-testid':'usage-hero'},h('div',{className:'au-hero-total'},
 h('div',{className:'au-big'},mode==='Cost'?selectedCost(s):t('total_tokens')),
 h('div',{className:'au-muted'},count(s?.sessions||0)+' sessions · '+(mode==='Cost'?'API-equivalent estimate':'processed tokens')),
 mode==='Cost'&&s?.supplemental_requests?h('div',{className:'au-muted','data-testid':'retrospective-overview-note'},count(s.supplemental_requests)+' past request(s) valued at currently published provider rates, not historical charges'):null,
 h('div',{className:'au-provider-totals'},...providerRows)),
 h(UsageChart,{data,mode,onSelectRange})):null,
 h('div',{className:'au-quality-line'},h('span',{className:'au-muted'},count(s?.attempts||0)+' requests · '+count(s?.pending||0)+' open · '+count(data?.compression_count||0)+' compressions'),
 h('details',{'data-testid':'usage-diagnostics'},h('summary',{},'Usage diagnostics'),
 h('div',{className:'au-muted'},count(s?.pending||0)+' open (owner observed live) · '+count(s?.unresolved||0)+' unresolved · '+count(s?.abandoned||0)+' abandoned'),
 h('div',{className:'au-muted'},count(s?.missing_usage||0)+' missing usage · '+count(s?.unpriced_requests||0)+' partially/unpriced'))))
}

function Breakdown({data,mode,onDrill,group,setGroup}){
 const minuteBuckets=(data?.trend?.seconds||86400)<3600,timeName=minuteBuckets?'2 minutes':data?.trend?.unit==='hour'?'Hour':'Day';
 // Only the time table is reversed; the shared chart keeps chronological buckets.
 // Other groups arrive ordered by their latest call across the full filtered set.
 const rows=group==='time'?[...(data.trend?.buckets||[])].sort((a,b)=>b.start-a.start):({model:data.model_groups,project:data.project_groups,session:data.session_groups,subagent:data.subagent_groups})[group]||[];
 const total=mode==='Cost'?Number(data.summary.known_cost_usd):data.summary.known.total_tokens;
 const grouping={model:'Model',time:timeName,project:'Project',session:'Session',subagent:'Subagents'};
 function name(g,drill=true){
  if(group==='model')return h(drill?'div':'span',{},g.model,
   h(drill?'div':'span',{className:'au-muted',...(!drill?{style:{display:'block'}}:{})},uiNames[g.provider]||g.provider));
  if(group==='time')return new Date(g.start*1000).toLocaleString(undefined,{timeZone:'UTC',month:'short',day:'numeric',...(minuteBuckets?{hour:'numeric',minute:'2-digit'}:timeName==='Hour'?{hour:'numeric'}:{})});
  const label=group==='project'?(g.label||'Unattributed project')+provenance(g):readable(g,'key');
  const drillable=nextDrillFilters(EMPTY_REQUEST_FILTERS,group,g)!==null;
  return h(drill&&drillable?'button':'span',{className:'au-drill',title:g.path||g.key,...(drill&&drillable?{onClick:()=>onDrill(group,g)}:{})},label,
   group==='subagent'?h('small',{},(g.agent_role||'subagent')+' · parent '+(originalId(g,'parent_session_id')||'unknown')):group==='session'?h('small',{},g.project_label||'Project unavailable'):h('small',{},g.basis?.includes('named_project')?'Named project':g.basis?.includes('working_directory')?'Working directory':g.basis?.includes('repository')?'Repository':'Metadata unavailable'));
 }
 const detail=group==='project'||group==='session'||group==='subagent';
 return h('section',{className:'au-breakdown','aria-label':'Usage breakdown'},h('div',{className:'au-toolbar','data-testid':'breakdown-controls',style:{justifyContent:'flex-end'}},h('div',{className:'au-segment au-breakdown-options',role:'group','aria-label':'Breakdown grouping'},...Object.entries(grouping).map(([g,label])=>h('button',{key:g,'aria-pressed':g===group,onClick:()=>setGroup(g)},label)))),
 rows.length?table([group==='time'?timeName+' (UTC)':grouping[group],'Cost · API estimate','Share','Processed tokens','Sessions',...(detail?['Subagents','Subagent tokens']:[])],rows.map(g=>{
  const value=mode==='Cost'?Number(g.known_cost_usd):g.known.total_tokens;
  return [name(g),h('span',{title:g.unpriced_requests?g.unpriced_requests+' incomplete/unpriced records':''},selectedCost(g),g.unpriced_requests?' *':''),total?(100*value/total).toFixed(1)+'%':'—',viewTokens(g,'total_tokens'),count(g.sessions),...(detail?[count(g.subagents||0),short(g.subagent_tokens||0)]:[])];
 }),null,rows.map(g=>JSON.stringify([group,g.key,g.provider,g.model,g.start])),rows.map(g=>({
  identity:name(g,false),summary:[
   {label:'Cost · API estimate',value:selectedCost(g)+(g.unpriced_requests?' *':''),title:g.unpriced_requests?g.unpriced_requests+' incomplete/unpriced records':undefined},
   {label:'Processed tokens'+(g.missing_fields?.total_tokens?' · known subtotal':''),value:viewTokens(g,'total_tokens'),title:g.missing_fields?.total_tokens?missingFieldNote(g,'total_tokens'):undefined}
  ]
 }))):h('p',{className:'au-muted'},'No recorded activity in this window.'))
}

// Request drill-downs are reversible local navigation. These fields narrow the
// current provider/time window; they never change or clear the ledger itself.
const EMPTY_REQUEST_FILTERS={agent:'',project:'',session:'',sessionScope:'family',subagentId:''};
function requestPageKey(context,filters){
 return JSON.stringify([context,filters.agent,filters.project,filters.session,filters.sessionScope,filters.subagentId]);
}
function nextDrillFilters(current,kind,row){
 const key=row?.key;
 if(!key||!['project','session','subagent'].includes(kind))return null;
 if(kind==='project')return {...current,project:key,session:'',subagentId:''};
 if(kind==='session')return originalId(row,'key')==='unattributed'?null:{...current,session:key,sessionScope:'family',subagentId:''};
 return {...current,subagentId:key,session:'',agent:'subagent'};
}
function RequestNavigation({filters,history,projects,onBack,onShowAll,onRemove,identityLabels}){
 const {agent,project,session,sessionScope,subagentId}=filters;
 if(!history.length&&!agent&&!project&&!session&&!subagentId)return null;
 const chips=[];
 const chip=(field,label,value)=>chips.push(h('button',{key:field,type:'button',className:'au-scope-chip',title:value,'aria-label':'Remove '+field+' filter',onClick:()=>onRemove(field)},h('span',{},label),h('span',{'aria-hidden':true},'×')));
 if(project){const match=(projects||[]).find(p=>p.id===project);chip('project','Project: '+(match?.label||project)+provenance(match),match?.path||project)}
 if(agent)chip('agent',({primary:'Primary agents',subagent:'Subagents only',unknown:'Unattributed agents'})[agent]||agent,agent);
 if(session)chip('session','Session: '+(identityLabels?.get(session)||session)+(sessionScope==='family'?' + descendants':''),session);
 if(subagentId)chip('subagent','Subagent: '+(identityLabels?.get(subagentId)||subagentId),subagentId);
 const previous=history[history.length-1];
 return h('nav',{className:'au-request-navigation','aria-label':'Request list navigation','data-testid':'request-navigation'},
  chips.length?h('div',{className:'au-active-scopes',role:'group','aria-label':'Active request filters'},...chips):null,
  h('div',{className:'au-return-actions'},
   h('button',{type:'button',className:'au-return-button','data-testid':'request-show-all',onClick:onShowAll,title:'Clear agent, project and session filters. Keep the current provider and time window.'},'Show all requests'),
   previous?h('button',{type:'button',className:'au-return-button','data-testid':'request-back',onClick:onBack,title:previous.tab==='Overview'?'Return to the previous breakdown':'Return to the previous request list','aria-label':'Back to previous view'},
    h('svg',{className:'au-back-icon',viewBox:'0 0 16 16',width:14,height:14,fill:'none',stroke:'currentColor',strokeWidth:1.6,strokeLinecap:'round',strokeLinejoin:'round','aria-hidden':true,focusable:'false'},h('path',{d:'M7 4 3 8l4 4M3 8h10'})),
    h('span',{className:'au-return-label'},'Back')):null));
}

function RequestView({data,more,back,offset,onDrill,details}){
 const rows=(data.requests||[]).map(r=>{const u=r.usage||{};return [h('span',{title:when(r.started)},compactWhen(r.started)),r.provider,r.response_model||r.model,
  r.session_id?h('button',{className:'au-drill',onClick:()=>onDrill('session',{key:r.session_id}),title:r.session_id},(r.original_ids?readable(r,'session_id'):r.session_id.slice(-18))):h('span',{},'Unattributed'+provenance(r)),h('span',{title:'Parent: '+(originalId(r,'parent_session_id')||'not reported')},r.agent_kind==='subagent'?'Subagent'+(r.agent_role?' · '+r.agent_role:''):r.agent_kind==='primary'?'Primary':'Unattributed'),h('span',{title:r.project_path||''},(r.project_label||'—')+provenance(r)),originalId(r,'task'),r.execution_state==='unresolved'?'unresolved (execution unknown)':r.execution_state==='owner_live'?'open (owner observed live)':r.status,count(u.input_tokens),count(u.output_tokens),count(u.cache_read_tokens),h('span',{title:SESSION_WRITE_BASIS},count(r.calculated_cache_writes?.tokens)),h('span',{title:r.supplemental_valuation?'Retrospective API-equivalent estimate using rates observed '+when(r.supplemental_valuation.observed_at)+'; original saved cost is in Details. Not a historical charge.':'Saved request cost estimate'},money(r.cost?.total_usd),r.supplemental_valuation?' †':''),tokensDetail(r,details)]})
 return h('div',{'data-testid':'request-list'},
  table(['Started','Provider','Model','Session','Agent','Project','Task','State','Uncached input','Output','Read',h('span',{title:SESSION_WRITE_BASIS},'Writes · calc.'),'Est. USD','Details'],rows,null,(data.requests||[]).map(r=>r.id),
   (data.requests||[]).map(r=>({identity:r.session_id?readable(r,'session_id'):'Unattributed session'+provenance(r),started:r.started,value:r.usage?.total_tokens==null?'Tokens unavailable':count(r.usage.total_tokens)+' tokens'}))),
 !rows.length?h('p',{className:'au-muted'},'No matching requests.'):null,
  h('div',{className:'au-toolbar'},offset>0?h('button',{onClick:back},'Previous requests'):null,data.next_offset!=null?h('button',{onClick:more},'Next requests'):null),
 h('p',{className:'au-muted'},`Showing ${data.requests.length?offset+1:0}–${offset+data.requests.length} of ${data.request_count} requests.`))
}

function CompressionView({data,mode,setMode,details}){
 const visible=(data.compressions||[]).filter(c=>mode==='all'||c.kind===mode);
 const rows=visible.map(c=>{
  const before=c.before_estimated_tokens,limit=c.context_limit,pc=limit&&before!=null?100*before/limit:null
  return [when(c.started)+provenance(c),c.kind,c.trigger,c.status==='pending'&&c.ended?'returned; no native outcome':c.status,
   h('div',{},count(before),pc!=null?h('div',{},h('div',{className:'au-meter'},h('i',{style:{width:Math.min(100,pc)+'%'}})),pc.toFixed(1)+'% of '+short(limit)):null),
   count(c.threshold_tokens),count(c.after_estimated_tokens),count(c.next_request_approx_tokens),
   h('div',{},count(c.next_request_prompt_tokens),h('div',{className:'au-muted'},originalId(c,'next_request_id')==='superseded'?'Superseded by another compaction':c.next_request_id?'First subsequent attempt':'Awaiting next attempt')),
   short(c.auxiliary?.known?.total_tokens),money(c.auxiliary?.known_cost_usd),tokensDetail(c,details)]
 })
 return h('section',{'data-testid':'compression-events','aria-label':'Compression events'},
 h('div',{className:'au-toolbar'},h('select',{value:mode,onChange:e=>setMode(e.target.value),'aria-label':'Compression type'},...['all','compression','micro_compaction'].map(v=>h('option',{key:v,value:v},v.replaceAll('_',' ')))),
 h('button',{onClick:()=>downloadFile('compression-events.json',JSON.stringify(data.compressions,null,2),'application/json')},'Export compression JSON')),
 h('div',{'data-testid':'compression-table'},table(['Started','Kind','Trigger','Outcome','Before (estimated)','Threshold','After (estimated)','Next call (estimated)','Next call input (reported)','Aux tokens (known)','Aux $ (known)','Details'],rows,null,visible.map(c=>c.id),
  visible.map(c=>({identity:readable(c,c.session_id?'session_id':'session_after'),started:c.started,value:h('span',{title:'Start: configured compression threshold (not measured starting usage). End: reported next-call input, which may include new content; not an exact post-compression size.'},'Start: '+count(c.threshold_tokens)+' · End: '+count(c.next_request_prompt_tokens)+' tokens')})))),
 !rows.length?h('p',{className:'au-muted'},'No compression events in this window.'):null,
 data.compression_truncated?notice('First 1,000 compression records shown. Narrow the time window for a complete compression export.'):null)
}
// This is a second control surface for the same page-level window, not an
// independent filter or another set of metrics. Custom dates update both copies.
function CacheWindowFilters({period,onPeriod,startValue,endValue,onStart,onEnd,window}){
 const choices={'1h':'Past hour','24h':'Past 24h','7d':'7 days','30d':'30 days','90d':'90 days',all:'All recorded',custom:'Custom'};
 return h('div',{className:'au-cache-window','data-testid':'cache-window-filters'},
  h('div',{className:'au-segment au-period-buttons',role:'group','aria-label':'Cache time window'},...Object.entries(choices).map(([value,label])=>h('button',{key:value,type:'button','aria-pressed':period===value,onClick:()=>onPeriod(value)},label))),
  h('select',{className:'au-period-select','aria-label':'Cache time window',value:period,onChange:e=>onPeriod(e.target.value)},...Object.entries({'1h':'Past hour','24h':'Past 24h','7d':'7 days','30d':'30 days','90d':'90 days',all:'All recorded',custom:'Custom'}).map(([value,label])=>h('option',{key:value,value},label))),
  period==='custom'?h('input',{type:'datetime-local',step:1,value:startValue,'aria-label':'Cache window start',onChange:e=>onStart(e.target.value)}):null,
  period==='custom'?h('input',{type:'datetime-local',step:1,value:endValue,'aria-label':'Cache window end',onChange:e=>onEnd(e.target.value)}):null,
  h('span',{className:'au-muted au-window-label'},window?new Date(window.start*1000).toLocaleString()+' — '+new Date(window.end*1000).toLocaleString():''));
}
function cacheCostValue(s){
 if(!s?.attempts)return money(0);
 return Object.keys(bucketNames).some(k=>(s.cost_missing_fields?.[k]??s.attempts)<s.attempts)?money(s.known_cost_usd):'—';
}
function cacheAmount(s){return h('span',{title:s?.unpriced_requests?'Known cost subtotal; '+count(s.unpriced_requests)+' requests pending, incomplete or unpriced':''},cacheCostValue(s),s?.unpriced_requests?' *':'')}
function rateContext(rate){
 if(!rate)return '—';
 if(rate.context_band&&rate.threshold_tokens!=null)return (rate.context_band==='long'?'>':'≤')+count(rate.threshold_tokens);
 if(rate.min_prompt_tokens)return '≥'+count(rate.min_prompt_tokens);
 return 'Standard';
}
// All four component rows and the total row are represented once, as cards.
// Counters/prices still use the same full-window backend aggregates.
function ComponentCostCards({summary:s}){
 const entries=[['total','Total'],...Object.entries(bucketNames)];
 return h('div',{className:'au-component-cards au-section-summary','data-testid':'component-cost-cards',role:'group','aria-label':'Token component costs'},...entries.map(([key,label])=>{
  const total=key==='total',writes=key==='cache_write_tokens',tokenKey=total?'total_tokens':key;
  const tokenMissing=total?(s.missing_usage||0):(s.missing_fields?.[key]||0);
  const priceMissing=total?(s.unpriced_requests||0):(s.cost_missing_fields?.[key]||0);
  const amount=total?cacheCostValue(s):s.attempts&&priceMissing>=s.attempts?'—':money(s.cost_components?.[key]);
  const lines=writes?
    [['Calculated tokens',sessionWrites(s,count),'tokens'],
     ['Read comparisons',count(s.session_cache_writes?.compared_requests||0),'comparisons'],
     ['Provider write cost',amount+(priceMissing?' *':''),'reported_cost'],
     ['Unavailable reported writes',count(tokenMissing),'missing'],
     ['Incomplete cost estimates',count(priceMissing),'unpriced']]:
    [[total?'Processed tokens':'Known tokens',viewTokens(s,tokenKey,count),'tokens'],
     ['Requests without this value',count(tokenMissing),'missing'],
     ['Incomplete cost estimates',count(priceMissing),'unpriced']];
  return h('article',{key,className:'au-cost-card','data-testid':'cost-card-'+key,'aria-label':label+' cost'},
   h('div',{className:'au-muted'},label),
   h('div',{className:'au-number','data-testid':'component-amount',title:writes?SESSION_WRITE_BASIS:total?'Full selected-window cost subtotal, not a sum of unit rates.':'API-equivalent cost at the saved request rates.'},writes?sessionWrites(s,count):amount,!writes&&priceMissing?' *':''),
   h('div',{className:'au-muted'},writes?sessionWriteNote(s):priceMissing?'API-equivalent cost · subtotal':'API-equivalent cost'),
   h('dl',{},...lines.flatMap(([name,value,field],i)=>[h('dt',{key:'dt'+i,title:field==='missing'?missingFieldNote(s,tokenKey):field==='unpriced'?'A token value or its saved unit rate is unavailable. This is not a count of lost requests.':undefined},name),h('dd',{key:'dd'+i,'data-field':field},value)])));
 }));
}
function SavingsCards({summary:s}){
 const spec=[['cache_read_savings_usd','Cache-read savings','Versus uncached input'],['cache_write_premium_usd','Cache-write premium','Extra cost versus uncached input'],['cache_savings_usd','Net cache savings','Read savings minus write premium']];
 return h('div',{className:'au-metrics au-section-summary','data-testid':'cache-savings-summary'},...spec.map(([key,label,note])=>{
  const missing=s.savings_missing?.[key]||0,covered=Math.max(0,(s.attempts||0)-missing);
  return h('div',{key,className:'au-metric',title:missing?'Known subtotal only. These savings cards can cover different requests; compare their coverage counts before subtracting them.':note},
   h('div',{className:'au-muted'},label),h('div',{className:'au-number'},savingsValue(s,key)),
   h('div',{className:'au-muted'},note),h('div',{className:'au-muted','data-testid':'savings-coverage'},count(covered)+' / '+count(s.attempts||0)+' requests'+(missing?' · subtotal':'')));
 }));
}
function CacheView({data,profile,provider,refresh,onError}){
 const [pricingBusy,setPricingBusy]=useState(false),s=data.summary,catalogs=data.price_catalogs||[];
 async function update(){if(isAllProfiles(profile))return;setPricingBusy(true);try{await rest('/ledger/pricing/refresh?profile='+encodeURIComponent(profile),{method:'POST'});refresh()}catch(e){onError(String(e.message||e))}finally{setPricingBusy(false)}}
 // Source is the applied saved rate or explicit retrospective read projection,
 // never a price inferred from the paginated request list.
 const applied=data.applied_rate_groups;
 const rows=(applied||[]).map(g=>{
  const r=g.rate,source=r?[r.retrospective?'Retrospective current-published-rate estimate; not a historical charge': 'Saved rate at request',r.source,r.source_url,r.observed_at?when(r.observed_at):r.pricing_version].filter(Boolean).join(' · '):'No rate: pending or unpriced request';
  return [uiNames[g.provider]||g.provider,h('span',{title:source},g.model),g.service_tier||'unspecified',rateContext(r),count(g.attempts),
   money(r?.input_tokens),money(r?.output_tokens),money(r?.cache_read_tokens),money(r?.cache_write_tokens),viewTokens(g,'total_tokens',count),cacheAmount(g)];
 });
 return h('section',{'data-testid':'cache-costs','aria-label':'Cache costs'},
  s.supplemental_requests?h('p',{className:'au-muted','data-testid':'retrospective-cost-note'},count(s.supplemental_requests)+' request(s) include retrospective API-equivalent estimates at currently observed provider rates, not historical charges. Original costs remain in request Details.'):null,
  h(ComponentCostCards,{summary:s}),
  h(SavingsCards,{summary:s}),
  h('section',{'data-testid':'published-rates','aria-label':'Applied rates and usage for the selected window',title:'Applied saved rates and labelled retrospective rates in USD per 1M tokens; usage and costs cover the full selected window'},
   applied==null?h('p',{className:'au-muted',role:'status'},'Restart the Hermes backend to load window-specific rates.'):
    table(['Provider','Exact model','Tier','Context','Requests','Uncached $ / 1M','Output $ / 1M','Read $ / 1M','Write $ / 1M','Processed tokens','Est. USD'],rows,
     ['Total','','','',''+count(s.attempts),'','','','',viewTokens(s,'total_tokens',count),cacheAmount(s)],
     (applied||[]).map(g=>JSON.stringify([g.provider,g.model,g.service_tier,g.rate])),
     (applied||[]).map(g=>({identity:[g.provider,g.model,g.service_tier,rateContext(g.rate)].filter(Boolean).join(' · '),value:viewTokens(g,'total_tokens',count)+' known tokens'}))),
   applied?.length===0?h('p',{className:'au-muted'},'No recorded requests in this window.'):null),
  !isAllProfiles(profile)&&h('div',{className:'au-toolbar','data-testid':'price-refresh-controls',style:{justifyContent:'flex-end',marginTop:16}},
   catalogs.some(p=>p.status==='unavailable'||p.stale)?h('small',{className:'au-muted',role:'status'},'Some prices unavailable or stale'):null,
   h('button',{onClick:update,disabled:pricingBusy,title:'Queue a public provider catalog refresh (at most once per minute per source). Existing saved costs remain fixed; previously unpriced requests can gain read-only retrospective estimates.'},pricingBusy?'Refreshing…':'Refresh provider prices')))
}


const QUOTA_PAGE='Subscriptions';
// Providers are the main destinations; detail pages never enter that navigation.
const QUOTA_HOME='__quota_home__';
const PROVIDER_SUBPAGES=['Overview','Requests','Cache & costs','Compressions','Models & tasks','Skills usage'];
const VIEW_KEY='usage-view-v1:';
const viewScope=selected=>isAllProfiles(selected)?'aggregate:all':selected?'profile:'+selected:'';
const PERIODS=['1h','24h','7d','30d','90d','all','custom'];
const GROUPS=['model','time','project','session','subagent'];
const SKILL_VIEWS=['Frequency','Context footprint','Session timeline'];
const safeId=value=>typeof value==='string'&&value.length<=160&&!/[\x00-\x1f]/.test(value)?value:'';
const option=(value,choices,fallback)=>choices.includes(value)?value:fallback;
function readView(profile){
 const defaults={provider:QUOTA_HOME,tab:'Overview',mode:'Tokens',period:'24h',customStart:'',customEnd:'',
  filterTest:'',compMode:'all',group:'model',filters:{...EMPTY_REQUEST_FILTERS},skillsView:'Frequency',skillsModel:'',skillsSkill:''};
 // An implicit server profile is not stable across backend/profile changes.
 if(!profile)return defaults;
 let raw;try{raw=storage?.get(VIEW_KEY+encodeURIComponent(profile),null)}catch{return defaults}
 if(!raw||typeof raw!=='object'||Array.isArray(raw)||raw.version!==1||raw.profile!==profile)return defaults;
 const filters=raw.filters&&typeof raw.filters==='object'&&!Array.isArray(raw.filters)?raw.filters:{};
 const datetime=value=>typeof value==='string'&&/^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?$/.test(value)&&!Number.isNaN(Date.parse(value))?value:'';
 const period=option(raw.period,PERIODS,'24h');
 const customStart=period==='custom'?datetime(raw.customStart):'';
 const customEnd=period==='custom'?datetime(raw.customEnd):'';
 return {...defaults,
  provider:raw.provider===''?'':raw.provider===QUOTA_HOME?QUOTA_HOME:safeId(raw.provider)||QUOTA_HOME,
  tab:option(raw.tab,PROVIDER_SUBPAGES,'Overview'),mode:option(raw.mode,['Tokens','Cost'],'Tokens'),period,
  customStart,customEnd:customStart&&customEnd&&Date.parse(customEnd)<Date.parse(customStart)?'':customEnd,
  filterTest:safeId(raw.filterTest),compMode:option(raw.compMode,['all','compression','micro_compaction'],'all'),
  group:option(raw.group,GROUPS,'model'),
  filters:{agent:option(filters.agent,['','primary','subagent','unknown'],''),project:safeId(filters.project),
   session:safeId(filters.session),sessionScope:option(filters.sessionScope,['family','exact'],'family'),subagentId:safeId(filters.subagentId)},
  skillsView:option(raw.skillsView,SKILL_VIEWS,'Frequency'),skillsModel:safeId(raw.skillsModel),skillsSkill:safeId(raw.skillsSkill)};
}
function saveView(profile,view){
 if(!profile)return;
 try{storage?.set(VIEW_KEY+encodeURIComponent(profile),{version:1,profile,...view})}catch{/* in-memory state remains usable */}
}
function tabKeys(event){
 const keys=['ArrowLeft','ArrowRight','Home','End'];
 if(!keys.includes(event.key)||event.target.getAttribute('role')!=='tab')return;
 const buttons=Array.from(event.currentTarget.querySelectorAll('[role="tab"]'));
 const index=buttons.indexOf(event.target);if(index<0)return;
 const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;
 event.preventDefault();buttons[next].focus();buttons[next].click();
}
function QuotaHome({quota,selected,hiddenIds,showHidden,onNavigate}){
 const data=quota.data,error=quota.error;
 if(error&&!data)return h(ErrorState,{title:'Could not load usage',description:String(error.message||error)},h(Button,{onClick:()=>quota.refetch()},'Retry'));
 if(!data)return h('div',{},h(Skeleton,{}),h(Skeleton,{}));
 if(isAllProfiles(selected))return h('section',{'data-testid':'quota-home'},notice('Combined subscription quota unavailable. Accounts may be shared across profiles; no quota probes are performed. Select an individual profile for its quota.'));
 const providers=Array.isArray(data.providers)?data.providers:[],visible=providers.filter(p=>!hiddenIds.includes(p.id)),hidden=providers.filter(p=>hiddenIds.includes(p.id));
 const live=visible.filter(p=>p.quota?.available).length,profile=selected||data.profile||'this profile';
 return h('section',{className:'au-quota-home','data-testid':'quota-home'},
  error?h('p',{className:'au-muted'},'Showing the last good response — refresh failed: '+String(error.message||error)):null,
  !data.profiles?h('p',{className:'au-muted'},'Older backend: restart Hermes to load profile selection.'):null,
  h('p',{className:'au-muted'},providers.length+' provider(s)'+((data.profiles||[]).length>1?' in '+profile:'')+' · '+live+' reporting live quota'+((data.profiles||[]).length>1?' · switch profile above to inspect another':'')),
  h(Separator,{}),h('div',{className:'flex flex-col gap-2'},...visible.map(p=>h(ProviderCard,{key:p.id,provider:p,onNavigate,profile})),...(showHidden?hidden.map(p=>h(ProviderCard,{key:'hidden-'+p.id,provider:p,isHidden:true,onNavigate,profile})):[]),
  !visible.length?h(EmptyState,{title:providers.length?'Every provider is hidden':'No providers to show',description:providers.length?'Use Hidden above to bring them back.':'This profile has no provider credentials and no recent usage.'}):null),
  h('p',{className:'au-muted'},"✕ on a card hides that provider (persisted, and excluded from the status-bar chip) · quota comes from each provider's own API using this profile's credentials."))
}
// Individual providers reuse the original quota card and profile query.
// Usage time/session/agent filters apply to the ledger, not the live allowance.
function ProviderLimits({quota,providerId,label,selected,hiddenIds=[]}){
 if(!providerId||providerId===QUOTA_HOME)return null;
 if(isAllProfiles(selected))return h('section',{'data-testid':'provider-limits'},notice('Combined subscription quota unavailable · select an individual profile.'));
 const data=quota.data;
 // The shared query can retain the previous profile while the next one loads.
 const wrongProfile=!!(selected&&data?.profile&&data.profile!==selected);
 const waiting=!data||wrongProfile||!!quota.isPlaceholderData;
 const matching=!waiting?(data.providers||[]).find(p=>p.id===providerId):null;
 const props={className:'au-provider-limits','data-testid':'provider-limits',
  'data-provider':providerId,'data-profile':selected||data?.profile||'',
  'aria-label':label+' subscription limits'};
 if(waiting)return h('section',props,quota.error
  ?h('div',{className:'au-provider-quota-status',role:'status'},h('span',{},'Quota unavailable'),
    h('button',{type:'button',onClick:()=>quota.refetch(),title:String(quota.error.message||quota.error)},'Retry quota'))
  :h('div',{'aria-busy':true,'aria-label':'Loading provider limits'},h(Skeleton,{})));
 const item=matching||{id:providerId,label,quota:{available:false,windows:[],details:[],
  unavailable_reason:'No quota data for this provider in this profile.'}};
 // Opening a hidden provider explicitly shows its card with the original Unhide
 // action; its Subscriptions/status-bar visibility remains controlled by that action.
 return h('section',props,h(ProviderCard,{provider:item,isHidden:hiddenIds.includes(providerId),profile:selected||data.profile}),
  quota.error?h('small',{className:'au-muted',role:'status',title:String(quota.error.message||quota.error)},'Quota refresh failed · last known value'):null);
}
// Connected is an observed producer lease, never inferred from an open quota card.
// The SDK socket has reconnect/backoff built in. REST stays available on OAuth
// remotes, where the SDK socket is a no-op. This never restarts a gateway.
const CONNECTION_QUERY_OPTIONS={retry:3,retryDelay:attempt=>Math.min(1000*2**attempt,30000),refetchInterval:15000,refetchIntervalInBackground:true,refetchOnReconnect:true};
const pendingLedgerReads=new Map();
function sharedLedgerRead(path,options,identity=path){
 // Full-window analytics can exceed a minute; recorder probes stay short.
 const route=path.split('?')[0];
 if(route==='/ledger'||route==='/ledger/skills')options={timeoutMs:120000,...options};
 const existing=pendingLedgerReads.get(identity);if(existing)return existing;
 const pending=Promise.resolve().then(()=>scopedRead(path,options)).finally(()=>{if(pendingLedgerReads.get(identity)===pending)pendingLedgerReads.delete(identity)});
 pendingLedgerReads.set(identity,pending);return pending;
}
let liveLedgerOwnerId=0;
// The token is a filesystem hint, not a database revision. Only a full REST
// response publishes accounting; a hint never modifies totals or event rows.
function makeLedgerCoordinator({profile,path,windowSeconds,publish,clock=globalThis}){
 let alive=true,active=false,dirty=false,manual=false,token=null,checking=false;
 let unsupported=false,failures=0,checkFailures=0,flight=0,followup=null,retry=null;
 const now=()=>clock.now?.()??Date.now();
 let lastFallback=now();
 const identity='live:'+ ++liveLedgerOwnerId;
 const scope=isAllProfiles(profile)?'all':'selected';
 const tokenPath='/ledger/change-token?'+new URLSearchParams(isAllProfiles(profile)?{profile_scope:'all'}:{profile_scope:'selected',profile:profile||''});
 const current=()=>alive;
 const clear=()=>{clock.clearTimeout(followup);clock.clearTimeout(retry);followup=retry=null};
 const check=async()=>{
  if(!alive||unsupported||checking)return null;
  checking=true;
  try{
   const result=await rest(tokenPath,{timeoutMs:8000});
   if(!alive)return null;
   if(result?.version!==1||result?.profile_scope!==scope||result?.read_only!==true||
      result?.token_kind!=='opaque-filesystem-hint'||typeof result?.change_token!=='string'||
      result?.capabilities?.change_check!==true)throw new Error('Unsupported change-check response');
   if(checkFailures){checkFailures=0;publish({checkLimited:false})}
   return result.change_token;
  }catch(error){
   if(alive){
    checkFailures++;
    if(error.status===404||error.status===405||/Unsupported change-check response/.test(String(error.message)))unsupported=true;
    if(unsupported||checkFailures>=3)publish({checkLimited:true});
   }
   return null;
  }finally{checking=false}
 };
 const schedule=(delay)=>{
  if(!alive||active||followup)return;
  followup=clock.setTimeout(()=>{followup=null;start()},delay);
 };
 async function start(){
  if(!alive||active)return;
  clear();active=true;dirty=false;
  publish({fetching:true,manual});
  let before=null,after=null,failed=false;
  try{
   // A busy check must not be used as evidence that a read acknowledged a hint.
   before=await check();
   const response=await sharedLedgerRead(rollingPath(path,windowSeconds),undefined,identity+':'+ ++flight);
   after=await check();
   if(!current())return;
   if(before!==null&&after!==null&&before!==after)dirty=true;
   // A token observed only after the read cannot certify this response.
   if(before!==null&&after===before)token=after;
   publish({data:response,error:null,fetching:false,manual});
   failures=0;
  }catch(error){
   if(!current())return;
   failed=true;failures++;
   publish({error,fetching:false,manual:false});
   manual=false;
  }finally{
   active=false;
   if(!alive)return;
   if(dirty){
    // Release the shared flight before starting a fresh read. Bursts collapse
    // into one follow-up, including a hint delivered during the post-read check.
    schedule(failed?Math.min(30000,5000*failures):400);
   }else if(failed){
    retry=clock.setTimeout(()=>{retry=null;start()},Math.min(30000,5000*failures));
   }else if(manual){manual=false;publish({manual:false})}
  }
 }
 function hint(){if(!alive)return;if(active){dirty=true;return}schedule(150)}
 async function checkForChanges(){
  if(!alive)return;
  if(unsupported){
   if(!windowSeconds&&document.visibilityState!=='hidden'&&now()-lastFallback>=60000){lastFallback=now();hint()}
   return;
  }
  if(checkFailures>=3){
   if(now()-lastFallback<60000)return;
   lastFallback=now();
   const recovered=await check();
   if(!alive)return;
   if(recovered!==null){if(token!==recovered)hint();return}
   if(!windowSeconds&&document.visibilityState!=='hidden')hint();
   return;
  }
  const next=await check();
  if(!alive||next===null)return;
  if(token===null){token=next;hint();return}
  if(next!==token)hint();
 }
 function refresh(){if(!alive)return;manual=true;publish({manual:true});hint()}
 function dispose(){alive=false;clear()}
 return {start,hint,refresh,checkForChanges,dispose,get unsupported(){return unsupported}};
}
function useLiveLedger({profile,path,view,enabled,windowSeconds}){
 const [state,setState]=useState({view:null,data:null,error:null,fetching:false,manual:false,checkLimited:false});
 const [owner]=useState(()=>({key:null,coordinator:null}));
 const key=JSON.stringify([pickerValue(profile),view,path,windowSeconds]);
 if(owner.key!==key){owner.coordinator?.dispose();owner.key=key;owner.coordinator=null}
 useEffect(()=>{
  if(!enabled)return;
  const coordinator=makeLedgerCoordinator({profile,path,windowSeconds,publish:patch=>{
   if(owner.key===key)setState(previous=>({...previous,...patch,view:key}));
  }});
  owner.coordinator=coordinator;
  coordinator.start();
  const interval=setInterval(()=>coordinator.checkForChanges(),isAllProfiles(profile)?30000:20000);
  const expiry=windowSeconds?setInterval(()=>{if(document.visibilityState!=='hidden')coordinator.hint()},60000):null;
  const visible=()=>{if(document.visibilityState==='visible'){coordinator.checkForChanges();if(windowSeconds)coordinator.hint()}};
  document.addEventListener('visibilitychange',visible);
  return()=>{coordinator.dispose();clearInterval(interval);clearInterval(expiry);document.removeEventListener('visibilitychange',visible);if(owner.coordinator===coordinator)owner.coordinator=null};
 },[key,enabled]);
 const current=state.view===key?state:{data:null,error:null,fetching:false,manual:false,checkLimited:false};
 return {...current,hint:()=>owner.coordinator?.hint(),refresh:()=>owner.coordinator?.refresh()};
}
function RefreshMenu({refresh,reload,busy}){
 const choose=action=>event=>{event.currentTarget.closest('details').removeAttribute('open');action()};
 return h('details',{className:'au-refresh-menu',onKeyDown:event=>{if(event.key==='Escape'){event.currentTarget.removeAttribute('open');event.currentTarget.querySelector('summary').focus()}}},
  h('summary',{'aria-label':'Refresh actions',title:'Refresh data or reload analytics code'},'▾'),
  h('div',{className:'au-refresh-options'},
   h('button',{type:'button',disabled:busy,onClick:choose(refresh)},'Refresh data'),
   h('button',{type:'button',disabled:busy,onClick:choose(reload),title:'Reload analytics readers in this backend process. Does not reload recorder hooks or restart agents.'},'Reload analytics backend')));
}
function useRecorderHealth({profile,enabled,onHint,readError,changeLimited}){
 const [control]=useState(()=>({alive:true,scope:profile,epoch:0,serial:0,busy:false,latest:{},onHint:null}));
 if(control.scope!==profile){control.scope=profile;control.epoch++;control.busy=false}
 control.onHint=onHint;
 const [health,setHealth]=useState(null),[operation,setOperation]=useState(null);
 const [transport,setTransport]=useState(null),[connectionEpoch,setConnectionEpoch]=useState(0);
 useEffect(()=>{control.alive=true;return()=>{control.alive=false;control.epoch++}},[]);
 async function probe(kind,path,identity=path){
  const epoch=control.epoch,serial=++control.serial;control.latest[kind]=serial;
  const current=()=>control.alive&&control.scope===profile&&control.epoch===epoch&&control.latest[kind]===serial;
  const publish=value=>{if(current())setHealth({scope:profile,epoch,received:Date.now(),...value})};
  try{const data=await sharedLedgerRead(path,kind==='status'?{timeoutMs:8000}:undefined,identity);publish({ok:true,data});return data}
  catch(error){publish({ok:false});throw error}
 }
 const status=()=>probe('status','/ledger/status?'+new URLSearchParams(scopeParams(profile)));
 // An old success cannot stay green indefinitely if fresh checks stop arriving.
 useEffect(()=>{
  if(isAllProfiles(profile)||!health?.ok||health.stale)return;
  const timer=setTimeout(()=>setHealth(value=>value===health?{...value,stale:true}:value),Math.max(0,30000-(Date.now()-health.received)));
  return()=>clearTimeout(timer);
 },[health]);
 useEffect(()=>{
  let live=true,timer,mode='native-events';
  const update=(state,detail)=>{if(live)setTransport({scope:profile,enabled,state,detail})};
  if(isAllProfiles(profile)){update('limited','All profiles uses read-only change checks; recorder health is profile-specific.');return()=>{live=false}}
  if(!enabled){update('not_needed','Live analytics subscription is idle on Subscriptions.');return()=>{live=false}}
  if(!socket){update('limited','Live update subscription unavailable; REST polling remains active.');return()=>{live=false}}
  update('connecting','Waiting for the live update subscription to acknowledge.');
  const deadline=ms=>{clearTimeout(timer);timer=setTimeout(()=>update('limited','No recent subscription acknowledgement; REST polling remains active.'),ms)};
  deadline(8000);
  let stop;
  try{stop=socket('/ledger/events?profile='+encodeURIComponent(profile),frame=>{
   if(!live||control.scope!==profile||!['connected','heartbeat','changed'].includes(frame?.type))return;
   if(frame.mode)mode=frame.mode;
   update(mode==='native-events'?'connected':'limited',mode==='native-events'?'Live update subscription acknowledged.':'Server is using display-refresh fallback.');deadline(45000);
   if(frame.type==='changed'||frame.type==='connected'){
    control.onHint?.();
    queryClient.invalidateQueries({queryKey:[ID,'skills']},{cancelRefetch:false});
   }
   if(frame.type==='connected')queryClient.invalidateQueries({queryKey:[ID,'connection',profile]},{cancelRefetch:false});
  })}catch(_){clearTimeout(timer);update('limited','Live update subscription failed; REST polling remains active.')}
  return()=>{live=false;clearTimeout(timer);if(typeof stop==='function')stop()};
 },[profile,enabled,connectionEpoch]);
 async function reconnect(){
  if(control.busy||!control.alive||control.scope!==profile)return false;
  control.busy=true;const epoch=++control.epoch;
  setOperation({scope:profile,epoch,busy:true});setConnectionEpoch(value=>value+1);
  control.onHint?.();
  const results=await Promise.allSettled([status()]);
  if(!control.alive||control.scope!==profile||control.epoch!==epoch)return false;
  control.busy=false;setOperation({scope:profile,epoch,busy:false});
  return results.every(result=>result.status==='fulfilled');
 }
 const checked=health?.scope===profile&&health.epoch===control.epoch?health:null;
 const link=transport?.scope===profile&&transport.enabled===enabled?transport:null;
 const pending=operation?.scope===profile&&operation.epoch===control.epoch&&operation.busy;
 let state='checking',label=pending?'Reconnecting':'Connecting',detail='Waiting for fresh recorder and data checks.';
 if(checked?.ok&&!checked.stale&&checked.data.state==='not_recording'){state='not_recording';label='Not recording';detail=checked.data.message||'No live recorder reporting for this profile.'}
 else if(checked?.ok===false||(enabled&&readError)){state='disconnected';label='Disconnected';detail='A fresh connection/data check failed. Retrying automatically.'}
 else if(!pending&&checked?.ok&&!checked.stale){
  const data=checked.data;state=data.state||'unverified';detail=[data.message,...(data.warnings||[]).slice(0,3)].filter(Boolean).join(' · ');
  if(state==='online'){
   if(changeLimited&&enabled){state='limited';detail+=' Change checks unavailable; visible-page 60-second fallback is active.'}
   else if(link?.state==='connecting'||!link){state='checking';detail='Recorder heartbeat verified; awaiting subscription acknowledgement.'}
   else if(link.state==='limited'){state='limited';detail+=' '+link.detail}
  }
  label=({online:'Online',limited:'Limited',not_recording:'Not recording',unverified:'Unverified',checking:'Reconnecting'})[state]||'Unverified';
 }else if(checked?.stale){state='disconnected';label='Disconnected';detail='Recorder status is stale; waiting for a fresh check.'}
 if(isAllProfiles(profile)){state=readError?'disconnected':'limited';label=pending?'Refreshing':readError?'Disconnected':changeLimited?'Limited':'Watching';detail=readError?'Read-only aggregate refresh failed; showing the last good response. Retrying automatically.':changeLimited?'Read-only · change checks unavailable; visible-page 60-second fallback.':'Read-only · change checks every 30 seconds; rolling windows expire on a visible-page minute. No aggregate events.'}
 return {status,reconnect,state,label,detail,readOnly:isAllProfiles(profile),busy:!!pending||link?.state==='connecting'};
}
function ConnectionBadge({connection,disabled}){
 const {state,label,detail,busy,reconnect,readOnly}=connection;
 const action=readOnly?'Refresh read-only data.':'Click to reconnect and refresh. Running agents are not restarted.';
 const title=detail+' '+action;
 return h('button',{type:'button',className:'au-connection','data-testid':'connection-status','data-state':state,onClick:reconnect,disabled:busy||disabled,title,'aria-label':'Usage recorder: '+label+'. '+action,'aria-busy':busy},
  h('span',{className:'au-connection-dot','aria-hidden':true}),h('span',{'aria-live':'polite'},label));
}

// Palette stays within the host theme; labels and counts never rely on colour.
const skillColours=['var(--ui-accent,#a799ef)','var(--ui-text-success,#64bba8)','var(--ui-text-warning,#d2b776)','var(--ui-text-error,#d58c96)','var(--ui-text-secondary,#b8b6cf)'];
function UsagePie({items,label,onChoose,selected}){
 const total=items.reduce((n,v)=>n+v.value,0);let angle=-Math.PI/2;
 return h('div',{className:'au-skill-chart'},
  total>0?h('svg',{viewBox:'0 0 200 200',className:'au-skill-pie',role:'img','aria-label':label},...items.map((item,i)=>{
   const start=angle;angle+=item.value/total*2*Math.PI;
   const props={key:item.id,fill:skillColours[i%skillColours.length],stroke:'var(--au-surface-bg)',strokeWidth:2,...(onChoose?{onClick:()=>onChoose(item.id),style:{cursor:'pointer'}}:{})};
   const title=h('title',{},item.label+': '+count(item.value)+' ('+(item.value/total*100).toFixed(1)+'%)');
   return item.value===total?h('circle',{...props,cx:100,cy:100,r:96},title):h('path',{...props,d:`M100 100 L${100+96*Math.cos(start)} ${100+96*Math.sin(start)} A96 96 0 ${angle-start>Math.PI?1:0} 1 ${100+96*Math.cos(angle)} ${100+96*Math.sin(angle)} Z`},title);
  })):h('p',{className:'au-muted'},'No recorded values in this selection.'),
  h('div',{className:'au-skill-legend'},...items.map((item,i)=>h(onChoose?'button':'div',{
   key:item.id,'data-scroll-key':'legend:'+item.id,className:'au-skill-legend-row',...(onChoose?{type:'button','aria-pressed':selected===item.id,onClick:()=>onChoose(item.id),'aria-label':item.label+' · '+count(item.value)+' loads · inspect'}:{})
  },h('span',{className:'au-skill-swatch',style:{background:skillColours[i%skillColours.length]},'aria-hidden':true}),h('span',{},item.label),h('strong',{},count(item.value)+' · '+(total?item.value/total*100:0).toFixed(1)+'%')))));
}
function SkillsUsageView({params,scope,onSession,profile,windowSeconds,view,setView,model,setModel,skill,setSkill}){
 const [offset,setOffset]=useState(0),[snapshotId,setSnapshotId]=useState('');
 const [scopeOwner]=useState(()=>({scope}));
 useEffect(()=>{if(scopeOwner.scope!==scope){scopeOwner.scope=scope;setModel('');setSkill('');setOffset(0);setSnapshotId('')}},[scope]);
 const p=new URLSearchParams(params);p.set('model',model);p.set('skill',skill);p.set('offset',String(offset));p.set('limit','50');
 const path='/ledger/skills?'+p,key=path;
 const query=useQuery({...readOptions(profile),queryKey:[ID,'skills',scope,key],queryFn:async()=>({...await sharedLedgerRead(rollingPath(path,windowSeconds),undefined,path),_skillsScope:key})});
 const [retained]=useState(()=>({scope:null,data:null}));
 const stableScope=JSON.stringify([scope,model,skill,offset]);
 const fresh=query.data?._skillsScope===key?query.data:null;
 if(fresh){retained.scope=stableScope;retained.data=fresh}
 // Rolling time bounds change the query key, not the user's selection. Keep
 // its keyed rows mounted while that refresh is pending; never bridge filters.
 const data=isAllProfiles(profile)&&query.error?null:fresh||(retained.scope===stableScope?retained.data:null);
 const chooseSkill=name=>{setSkill(name===skill?'':name);setOffset(0)};
 const kinds={skill_load:'Skill load',context_snapshot:'Before request',compression_before:'Before compression',compression_after:'After compression',turn_end:'Turn completed'};
 const eventLabel=e=>e.kind==='skill_load'?(e.success===false?'Failed load':e.is_reference?'Reference read':e.repeat?'Repeat skill load':'Skill load'):(kinds[e.kind]||e.kind);
 const snapshots=data?.snapshots||[];
 const snapshot=snapshotId?snapshots.find(s=>s.id===snapshotId):(snapshots.find(s=>s.context_used!=null)||snapshots[0]);
 const categories=(snapshot?.categories||[]).filter(c=>Number.isFinite(c.tokens)&&c.tokens>0);
 const inspected=(data?.skills||[]).find(s=>s.name===skill);
 const sessionLink=e=>e.session_id?h('button',{className:'au-drill',onClick:()=>onSession(e.session_id),title:'Filter all analytics to this session'},readable(e,'session_id')):'Unattributed';
 const eventRows=(data?.events||[]).map(e=>[
  when(e.ts),eventLabel(e),readable(e,'skill'),e.file_path||'Main file / not applicable',e.provider||'Unknown',e.model||'Unknown',
  e.project_label||originalId(e,'project_id')||'Unattributed',sessionLink(e),e.agent_kind||'Unattributed',
  e.estimated_tokens==null?'Not recorded':'~'+count(e.estimated_tokens),e.context_used==null?'—':'~'+count(e.context_used),e.source||'Not recorded',originalId(e,'compression_id')||'—',
  e.kind==='skill_load'?'—':h('button',{onClick:()=>{setView('Context footprint');setSnapshotId(e.id)}},'Inspect snapshot')
 ]);
 const eventHeaders=['Recorded at','Event','Skill','File','Provider','Model','Project','Session','Agent','Returned text · estimated tokens','Context footprint','Measurement source','Compression ID','Snapshot'];
 const eventList=(accordions=false)=>h('div',{'data-testid':'skill-events',key:stableScope},
  accordions?h('div',{className:'au-timeline-scroll',tabIndex:0,'aria-label':'Session timeline entries'},...(data?.events||[]).map((e,i)=>h('details',{key:e.id,'data-event-id':e.id,'data-scroll-key':'event:'+e.id,className:'au-timeline-entry'},
   h('summary',{},h('strong',{className:'au-timeline-session'},e.session_id?readable(e,'session_id'):'Unattributed session'+provenance(e)),
    h('span',{},h('span',{className:'au-muted'},'Context footprint'),h('br'),e.context_used==null?'Not recorded':'~'+count(e.context_used)+' tokens'),
    h('span',{},h('span',{className:'au-muted'},'Recorded'),h('br'),when(e.ts))),
   h('dl',{},...eventRows[i].map((value,j)=>h('div',{key:j},h('dt',{},eventHeaders[j]),h('dd',{},value))))))):
  table(eventHeaders,eventRows,null,(data?.events||[]).map(e=>e.id)),
  !(data?.events||[]).length?h('p',{className:'au-muted'},'No recorded events match this selection.'):null,
  h('div',{className:'au-toolbar'},h('span',{className:'au-muted'},count(data?.event_count)+' matching events'),
   h('button',{disabled:offset===0,onClick:()=>setOffset(Math.max(0,offset-50))},'Previous events'),
   h('button',{disabled:data?.next_offset==null,onClick:()=>setOffset(data.next_offset)},'Next events')));
 return h('section',{'data-testid':'skills-usage','aria-label':'Skills usage'},
  h(Coverage,{data}),
  h('div',{className:'au-toolbar'},h('div',{className:'au-segment',role:'group','aria-label':'Skills view'},...['Frequency','Context footprint','Session timeline'].map(v=>h('button',{key:v,'aria-pressed':view===v,onClick:()=>{setView(v);setOffset(0)}},v))),
   h('label',{},'Model ',h('select',{'aria-label':'Skills model',value:model,onChange:e=>{setModel(e.target.value);setOffset(0);setSnapshotId('')}},h('option',{value:''},'All models'),...[...new Set([...(data?.model_options||[]),...(model?[model]:[])])].map(m=>h('option',{key:m,value:m},m))))),

  query.error?h('div',{className:'au-notice',role:'alert'},'Skills history unavailable. Newly installed recorder/API code requires a backend and producer restart. ',h('button',{onClick:()=>query.refetch()},'Retry skills history')):null,
  !data?h('p',{className:'au-muted',role:'status'},query.error?'No history shown for this selection.':'Loading skills history…'):h('div',{},
   view==='Context footprint'?h('div',{className:'au-toolbar au-snapshot-picker'},h('label',{},'Snapshot ',h('select',{'aria-label':'Context snapshot',value:snapshotId,onChange:e=>setSnapshotId(e.target.value)},h('option',{value:''},'Latest measured snapshot'),...snapshots.map(s=>h('option',{key:s.id,value:s.id},when(s.ts)+' · '+eventLabel(s)+' · '+(s.session_id?readable(s,'session_id'):'Unattributed'+provenance(s))))))):null,
   h('div',{className:'au-metrics'},metric('Recorded main-skill loads',data.coverage?.status==='not_recorded'?'—':count(data.summary?.loads)),metric('Reference reads',data.coverage?.status==='not_recorded'?'—':count(data.summary?.references)),metric('Failed loads',data.coverage?.status==='not_recorded'?'—':count(data.summary?.failures))),
   skill?h('div',{className:'au-toolbar'},h('strong',{},'Inspecting '+(inspected?readable(inspected,'name'):'selected skill')),h('button',{onClick:()=>{setSkill('');setOffset(0)}},'Clear skill selection')):null,
   view==='Frequency'?h('div',{},
    h('div',{className:'au-box','data-testid':'skill-frequency'},h('h3',{},'Most frequently loaded skills'),
     h(UsagePie,{label:'Skill load frequency',items:(data.skills||[]).filter(s=>s.loads>0).map(s=>({id:s.name,label:readable(s,'name'),value:s.loads})),selected:skill,onChoose:chooseSkill})),
    (data.skills||[]).some(s=>!s.loads)?h('div',{className:'au-toolbar'},h('span',{className:'au-muted'},'Reference-only or failed loads:'),...(data.skills||[]).filter(s=>!s.loads).map(s=>h('button',{key:s.name,'aria-pressed':skill===s.name,onClick:()=>chooseSkill(s.name)},readable(s,'name')))):null,
    skill?h('div',{className:'au-box','data-testid':'skill-drilldown'},h('h3',{},inspected?readable(inspected,'name'):'Selected skill'),h('p',{className:'au-muted'},count(inspected?.loads)+' successful loads · '+count(inspected?.sessions)+' distinct sessions · '+count(inspected?.repeat_loads)+' repeat loads · '+(inspected?.estimated_tokens==null?'Text size not recorded':'~'+count(inspected.estimated_tokens)+' returned tokens (estimate, not billing)')),eventList()):null):null,
   view==='Context footprint'?h('div',{'data-testid':'skill-context'},

    data.snapshots_truncated?h('p',{className:'au-muted'},'Showing the newest '+count(snapshots.length)+' of '+count(data.snapshot_count)+' snapshots. Narrow the time window or session to inspect older snapshots.'):null,
    snapshot?h('div',{className:'au-box'},h('h3',{},eventLabel(snapshot)+' · '+when(snapshot.ts)),h('p',{},sessionLink(snapshot),' · '+(snapshot.model||'Unknown model')),h('p',{},'Context: '+(snapshot.context_used==null?'Not recorded':'~'+count(snapshot.context_used))+(snapshot.context_max?' / '+count(snapshot.context_max):'')+' tokens'),
     snapshot.compression_id?h('div',{className:'au-toolbar'},h('span',{className:'au-muted'},'Compression '+originalId(snapshot,'compression_id')),...snapshots.filter(s=>s.compression_id===snapshot.compression_id&&s.id!==snapshot.id).map(s=>h('button',{key:s.id,onClick:()=>setSnapshotId(s.id)},eventLabel(s)+' · '+(s.context_used==null?'Not recorded':count(s.context_used)+' tokens')))):null,
     h(UsagePie,{label:'Estimated context composition',items:categories.map(c=>({id:c.id,label:c.label,value:c.tokens}))})):
     h('p',{className:'au-muted'},snapshotId?'This snapshot is outside the returned window. Choose another snapshot.':'No context snapshots recorded for this selection.')):null,
   view==='Session timeline'?h('div',{'data-testid':'skill-timeline'},eventList(true)):null
  ));
}

function UsagePage(){
 const selected=useValue($profile);
 // Remount profile-local filters, details and async controls synchronously, before
 // any request for the new scope can inherit a qualified ID from the old one.
 return h(UsagePageScope,{key:pickerValue(selected),selected});
}
function UsagePageScope({selected}){
 const [saved]=useState(()=>readView(viewScope(selected)));
 const [providerChoice,setProvider]=useState(saved.provider),[tab,setTab]=useState(saved.tab);
 const [restoringProvider,setRestoringProvider]=useState(()=>!!saved.provider&&saved.provider!==QUOTA_HOME);
 const aggregate=isAllProfiles(selected);
 const inventory=useQuery({queryKey:[ID,'profiles'],queryFn:()=>sharedLedgerRead('/ledger/profiles'),staleTime:60000,retry:false,refetchOnWindowFocus:false});
 const [showHidden,setShowHidden]=useState(false), hiddenIds=useValue($hidden),chipProvider=useValue($chipProvider)
 const quota=useUsage(selected,REFRESH_PAGE_MS)
 // A saved provider ID is not authority to query the ledger. Wait for the
 // selected profile's quota catalogue; never borrow another profile's rows.
 const quotaCurrent=!!quota.data&&!quota.isPlaceholderData&&
  (aggregate?quota.data.profile_scope==='all':!selected||quota.data.profile===selected);
 const available=quotaCurrent&&Array.isArray(quota.data.providers)
  ?quota.data.providers.some(row=>row&&typeof row.id==='string'&&row.id===providerChoice):false;
 // A cached match is safe; an absent ID, wrong-profile response or retry error
 // cannot become a fallback until the current catalogue read has settled.
 const catalogueReady=available||(!quota.isFetching&&(!!quota.error||!!quota.data));
 const provider=restoringProvider?(catalogueReady&&available?providerChoice:QUOTA_HOME):providerChoice;
 useEffect(()=>{
  if(restoringProvider&&catalogueReady){setProvider(provider);setRestoringProvider(false)}
 },[restoringProvider,catalogueReady,provider]);
 const [displayMode,setDisplayMode]=useState(saved.mode),[period,setPeriod]=useState(saved.period),[anchor,setAnchor]=useState(()=>Date.now()/1000),[customStart,setCustomStart]=useState(saved.customStart),[customEnd,setCustomEnd]=useState(saved.customEnd),[filterTest,setFilterTest]=useState(saved.filterTest),[err,setErr]=useState(''),[compMode,setCompMode]=useState(saved.compMode),[testId,setTestId]=useState(''),[busy,setBusy]=useState(false)
 const [requestFilters,setRequestFilters]=useState(()=>saved.filters);
 const {agent,project,session,sessionScope,subagentId}=requestFilters;
 const [breakdownGroup,setBreakdownGroup]=useState(saved.group);
 const [skillsView,setSkillsView]=useState(saved.skillsView),[skillsModel,setSkillsModel]=useState(saved.skillsModel),[skillsSkill,setSkillsSkill]=useState(saved.skillsSkill);
 useEffect(()=>{if(restoringProvider&&!catalogueReady)return;saveView(viewScope(selected),{provider,tab,mode:displayMode,period,customStart,customEnd,filterTest,
  compMode,group:breakdownGroup,filters:requestFilters,skillsView,skillsModel,skillsSkill})},
  [selected,provider,restoringProvider,catalogueReady,tab,displayMode,period,customStart,customEnd,filterTest,compMode,breakdownGroup,requestFilters,skillsView,skillsModel,skillsSkill]);
 const [drillTrail,setDrillTrail]=useState({context:'',entries:[]});
 const [requestPage,setRequestPage]=useState({key:'',offset:0});
 // The rolling clock is intentionally excluded: refreshing live totals must not
 // discard Back history. A changed provider/profile/test/window does invalidate it.
 const navigationContext=JSON.stringify([selected,provider,period,customStart,customEnd,filterTest]);
 const pageKey=requestPageKey(navigationContext,requestFilters);
 const offset=requestPage.key===pageKey?requestPage.offset:0;
 const setOffset=value=>setRequestPage({key:pageKey,offset:Math.max(0,value)});
 const drillHistory=drillTrail.context===navigationContext?drillTrail.entries:[];
 useEffect(()=>{setDrillTrail({context:navigationContext,entries:[]})},[navigationContext]);
 function editRequestFilters(patch){
  const next={...requestFilters,...patch};
  setRequestFilters(next);setDrillTrail({context:navigationContext,entries:[]});
  setRequestPage({key:requestPageKey(navigationContext,next),offset:0});
 }
 function clearRequestFilters(){editRequestFilters(EMPTY_REQUEST_FILTERS)}
 function showAllRequests(){detailEntries.clear();clearRequestFilters();setTab('Requests')}
 function removeRequestFilter(field){
  if(field==='session')editRequestFilters({session:'',sessionScope:'family'});
  else if(field==='subagent')editRequestFilters({subagentId:''});
  else if(field==='agent')editRequestFilters({agent:'',subagentId:''});
  else if(field==='project')editRequestFilters({project:''});
 }
 function returnFromDrill(){
  const previous=drillHistory[drillHistory.length-1];if(!previous)return;
  setRequestFilters(previous.filters);setTab(previous.tab);setBreakdownGroup(previous.group);
  setRequestPage({key:requestPageKey(navigationContext,previous.filters),offset:previous.offset});
  setDrillTrail({context:navigationContext,entries:drillHistory.slice(0,-1)});
 }
 const isQuota=provider===QUOTA_HOME;
 const ranges={'1h':3600,'24h':86400,'7d':604800,'30d':2592000,'90d':7776000,all:0}
 const initialStart=period==='custom'?(customStart?new Date(customStart).getTime()/1000:0):(ranges[period]?anchor-ranges[period]:0)
 const start=Number.isFinite(initialStart)?Math.max(0,initialStart):0,end=period==='custom'&&customEnd?new Date(customEnd).getTime()/1000:undefined
 const params=new URLSearchParams({...scopeParams(selected),provider:isQuota?'':provider,start:String(start),limit:'200',offset:String(offset),session,session_scope:sessionScope,agent,project,subagent:subagentId});if(end)params.set('end',String(end));if(filterTest)params.set('test_id',filterTest)
 const readScope=JSON.stringify([navigationContext,requestFilters,offset]);
 const windowSeconds=period!=='custom'?ranges[period]:0;
 const ledger=useLiveLedger({profile:selected,path:'/ledger?'+params,view:readScope,enabled:!isQuota,windowSeconds});
 const connection=useRecorderHealth({profile:selected,enabled:!isQuota,onHint:ledger.hint,readError:ledger.error,changeLimited:ledger.checkLimited});
 useQuery({...CONNECTION_QUERY_OPTIONS,queryKey:[ID,'connection',selected],queryFn:connection.status});
 const [reloadControl]=useState(()=>({busy:false,alive:true,scope:selected}));reloadControl.scope=selected;
 const [reloadBusy,setReloadBusy]=useState(false),[reloadNotice,setReloadNotice]=useState(null);
 useEffect(()=>{reloadControl.alive=true;return()=>{reloadControl.alive=false}},[]);
 async function reloadAnalytics(){
  if(aggregate||reloadControl.busy||connection.busy)return;
  reloadControl.busy=true;setReloadBusy(true);setReloadNotice({scope:selected,text:'Validating analytics code…'});
  let switched=false;
  const message=text=>{if(reloadControl.alive&&reloadControl.scope===selected)setReloadNotice({scope:selected,text})};
  try{
   const result=await rest('/ledger/analytics/reload?profile='+encodeURIComponent(selected),{method:'POST',timeoutMs:30000});
   switched=true;
   const active=await rest('/ledger/analytics?profile='+encodeURIComponent(selected),{timeoutMs:8000});
   if(!result.revision||result.revision!==active.revision)throw new Error('Active revision changed before verification.');
   message('Analytics '+active.revision.slice(0,12)+' loaded. Refreshing data…');
   const healthy=reloadControl.alive&&reloadControl.scope===selected?await connection.reconnect():false;
   message('Analytics '+active.revision.slice(0,12)+' loaded.'+(healthy?' Data refresh queued; status verified.':' Status check incomplete; data refresh queued.'));
  }catch(error){
   message(switched?'Reload was accepted, but follow-up verification failed. Refresh to check the active backend.':
    (error.status===404||error.status===405||/404|405/.test(String(error.message)))?'Reload support is not active yet. One backend restart is required.':String(error.message||'Analytics reload failed; previous code remains active.'));
  }finally{reloadControl.busy=false;if(reloadControl.alive)setReloadBusy(false)}
 }
 const [ledgerCache]=useState(()=>({scope:'',data:null}));
 const [detailEntries]=useState(()=>new Map());
 const [identityLabels]=useState(()=>new Map());
 const currentData=ledger.data;
 if(currentData){ledgerCache.scope=readScope;ledgerCache.data=currentData}
 const data=currentData||(ledgerCache.scope===readScope?ledgerCache.data:null),providers=quotaCurrent&&Array.isArray(quota.data.providers)?quota.data.providers:[];
 for(const row of [...(data?.requests||[]),...(data?.session_groups||[]),...(data?.subagent_groups||[])]){
  for(const field of ['session_id','subagent_id','key'])if(row[field])identityLabels.set(row[field],readable(row,field));
 }
 const detailContext={scope:selected,entries:detailEntries};
 const labels=Object.fromEntries(providers.map(p=>[p.id,p.label]));labels['openai-codex']='Codex';labels['nous']='Nous Portal'
 const names=[...new Set([...providers.map(p=>p.id),...(data?.providers||[]),...(!isQuota&&provider?[provider]:[])])].filter(v=>v&&v!==QUOTA_HOME)
 const label=provider?(labels[provider]||provider):'All providers'
 const [manualQuotaBusy,setManualQuotaBusy]=useState(false);
 const refresh=()=>{if(reloadControl.busy||ledger.manual||manualQuotaBusy)return;pendingRefresh=true;
  if(isQuota){setManualQuotaBusy(true);Promise.allSettled([inventory.refetch({cancelRefetch:false}),quota.refetch({cancelRefetch:false})]).finally(()=>{if(reloadControl.alive)setManualQuotaBusy(false)});return}
  inventory.refetch({cancelRefetch:false});
  ledger.refresh();queryClient.invalidateQueries({queryKey:[ID,'connection',selected]},{cancelRefetch:false});
  queryClient.invalidateQueries({queryKey:[ID,'skills']},{cancelRefetch:false});
  quota.refetch({cancelRefetch:false});
 }
 async function exportAll(){setBusy(true);setErr('');try{
   const rows=[],seen=new Set();let offset=0,signature=null,exportCoverage=null;
   const frozen=new URLSearchParams(params);
   frozen.set('start',String(data.window.start));frozen.set('end',String(data.window.end));
   for(;;){
    if(!reloadControl.alive||reloadControl.scope!==selected)throw new Error('Export cancelled: profile changed.');
    const p=new URLSearchParams(frozen);p.set('limit','2000');p.set('offset',String(offset));
    const d=await sharedLedgerRead('/ledger?'+p);
    if(!reloadControl.alive||reloadControl.scope!==selected)throw new Error('Export cancelled: profile changed.');
    if(!d.summary||d.request_count==null)throw new Error('Export unavailable: no readable recorded usage.');
    if(aggregate)exportCoverage=d.coverage;
    const next=JSON.stringify([d.request_count,aggregate?d.coverage:null,aggregate?d.profile_sequences:d.seq,(d.price_catalogs||[]).map(c=>[c.profile_id,c.source_id,c.snapshot_sha256,c.snapshot_at])]);
    if(signature!==null&&signature!==next)throw new Error('Export stopped: profile coverage or ledger sequences changed between pages (including provider catalog). Refresh and retry; no CSV was downloaded.');
    signature=next;
    for(const row of d.requests){if(seen.has(row.id))throw new Error('Export stopped: page membership changed. Refresh and retry.');seen.add(row.id);rows.push(row)}
    if(d.next_offset==null){if(rows.length!==d.request_count)throw new Error('Export stopped: returned count changed. Refresh and retry.');break}
    if(d.next_offset<=offset||!d.requests.length)throw new Error('Export stopped: invalid pagination.');
    offset=d.next_offset;
   }
   downloadFile('hermes-request-ledger.csv',csvText(rows.map(r=>({profile:r.profile||(!aggregate?selected:''),profile_id:r.profile_id||'',original_ids:r.original_ids||null,...(aggregate?{export_atomic:false,profile_coverage:exportCoverage}:{}),started_utc:new Date(r.started*1000).toISOString(),ended_utc:r.ended?new Date(r.ended*1000).toISOString():'',id:r.id,provider:r.provider,model:r.response_model||r.model,session_id:r.session_id,agent_kind:r.agent_kind,parent_session_id:r.parent_session_id,root_session_id:r.root_session_id,subagent_id:r.subagent_id,agent_role:r.agent_role,project:r.project_label,project_source:r.project_source,project_id:r.project_id,task:r.task,status:r.status,source:r.source,service_tier:r.service_tier,returned_service_tier:r.returned_service_tier,...r.usage,cache_write_tokens:r.calculated_cache_writes?.tokens??null,cache_write_method:'session_read_delta',provider_cache_write_tokens:r.usage?.cache_write_tokens??null,calculated_cache_writes:r.calculated_cache_writes||null,cost:r.cost,stored_accounting:r.stored_accounting||null,supplemental_valuation:r.supplemental_valuation||null,compression_id:r.compression_id,cache_read_change:r.cache_read_change||null}))))
  if(aggregate)setErr('CSV exported: '+rows.length+' rows · '+exportCoverage?.status+' profile coverage. Independent page/profile snapshots, not an atomic export; concurrent changes may require a retry.');
 }catch(e){if(reloadControl.alive)setErr(String(e.message||e))}finally{if(reloadControl.alive)setBusy(false)}}
 async function test(action){if(aggregate)return;setErr('');try{const d=await rest('/ledger/tests?profile='+encodeURIComponent(selected),{method:'POST',body:{action,id:testId,label:'Reset test '+new Date().toLocaleString()}});if(action==='start'){setTestId(d.id);setFilterTest(d.id);setPeriod('custom');setCustomStart(new Date(d.started*1000-new Date().getTimezoneOffset()*60000).toISOString().slice(0,19));setCustomEnd('')}else{setCustomEnd(new Date(d.ended*1000-new Date().getTimezoneOffset()*60000).toISOString().slice(0,19));setTestId('')}ledger.hint()}catch(e){setErr(String(e.message||e))}}
 function chooseTest(id){const t=data.tests.find(t=>t.id===id);if(!t)return;setTestId(t.ended?'':t.id);setFilterTest(t.id);setPeriod('custom');const local=x=>new Date(x*1000-new Date().getTimezoneOffset()*60000).toISOString().slice(0,19);setCustomStart(local(t.started));setCustomEnd(t.ended?local(t.ended):'')}
 function changePeriod(v){setPeriod(v);setFilterTest('');setAnchor(Date.now()/1000)}
 function selectChartRange(lo,hi){
  const local=t=>{const date=new Date(t*1000);return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,19)};
  setCustomStart(local(lo));setCustomEnd(local(hi));setFilterTest('');setPeriod('custom');
 }
 function drill(kind,g){
  const next=nextDrillFilters(requestFilters,kind,g);if(!next)return;
  const nextKey=requestPageKey(navigationContext,next);
  if(tab==='Requests'&&nextKey===pageKey)return; // No duplicate self-navigation.
  const previous={filters:{...requestFilters},tab,group:breakdownGroup,offset};
  setDrillTrail({context:navigationContext,entries:[...drillHistory,previous].slice(-50)});
  setRequestFilters(next);setRequestPage({key:nextKey,offset:0});setTab('Requests');
 }
 const header=h(PageHeader,{profiles:inventory.data?.profiles||discoveredProfiles?.profiles||quota.data?.profiles||[],profile:selected||inventory.data?.profiles?.find(p=>p.is_server)?.name||quota.data?.profile||'',setProfile:selectProfile,chipProviders:providers.filter(p=>!hiddenIds.includes(p.id)),chipProvider,setChipProvider:selectChipProvider,isFetching:ledger.manual||manualQuotaBusy,refetch:refresh,refreshBusy:reloadBusy||connection.busy||ledger.manual||manualQuotaBusy,refreshMenu:aggregate?null:h(RefreshMenu,{refresh,reload:reloadAnalytics,busy:reloadBusy||connection.busy||ledger.manual||manualQuotaBusy}),hiddenCount:hiddenIds.length,showHidden,setShowHidden,meta:aggregate?'All profiles · read-only · change checks':quota.data?`fetched ${fmtIst(quota.data.generated_at)} IST · probed in ${quota.data.probe_seconds}s`:null})
 const pageHeader=h('div',{},h('div',{className:'au-page-header'},h('div',{className:'au-original-header'},header),h(ConnectionBadge,{connection:aggregate?{...connection,reconnect:refresh}:connection,disabled:reloadBusy||aggregate&&ledger.manual})),inventory.error?notice('Profile discovery unavailable. A user-managed backend restart may be required; existing individual choices are retained.'):null,reloadNotice?.scope===selected?h('div',{className:'au-muted',role:'status','data-testid':'analytics-reload-result','aria-live':'polite'},reloadNotice.text):null);
 const mainItems=[{id:QUOTA_HOME,label:QUOTA_PAGE},{id:'',label:'All providers'},...names.map(id=>({id,label:labels[id]||id}))];
 const mainId=id=>'au-main-'+encodeURIComponent(id||'all');
 const subId=name=>'au-subpage-'+name.toLowerCase().replace(/[^a-z]+/g,'-');
 // The original quota page is a sibling of provider pages, not of their subpages.
 const mainNav=h('div',{'data-testid':'main-navigation'},
  h('nav',{className:'au-tabs au-main-tabs au-provider-tabs','aria-label':'Providers'},
   h('div',{role:'tablist','aria-label':'Provider pages',style:{display:'contents'},onKeyDown:tabKeys},...mainItems.map(item=>h('button',{key:item.id||'all',id:mainId(item.id),role:'tab','aria-selected':provider===item.id,'aria-controls':'au-main-panel',tabIndex:provider===item.id?0:-1,onClick:()=>setProvider(item.id)},item.label)))),
  h('select',{className:'au-provider-select','aria-label':'Main page',value:provider,onChange:e=>setProvider(e.target.value)},...mainItems.map(item=>h('option',{key:item.id||'all',value:item.id},item.label))));
 const subNav=h('nav',{className:'au-tabs au-subpage-tabs','aria-label':'Provider subpages','data-testid':'provider-subnavigation'},
  h('div',{role:'tablist','aria-label':'Provider subpages',style:{display:'contents'},onKeyDown:tabKeys},...PROVIDER_SUBPAGES.map(v=>h('button',{key:v,id:subId(v),role:'tab','aria-selected':tab===v,'aria-controls':'au-subpage-panel',tabIndex:tab===v?0:-1,onClick:()=>setTab(v)},v))));
 if(isQuota)return h(ScrollArea,{className:'h-full'},h('div',{className:'au-ledger p-4'},h('style',{},ledgerCss),pageHeader,mainNav,
  h('section',{id:'au-main-panel',role:'tabpanel','aria-labelledby':mainId(QUOTA_HOME)},h(QuotaHome,{quota:quotaCurrent&&Array.isArray(quota.data.providers)?quota:{...quota,data:null,error:catalogueReady?quota.error||new Error('Provider catalogue unavailable for this profile.'):null},selected,hiddenIds,showHidden,onNavigate:setProvider}))))
 let body=null
 if(data?.summary){
   if(tab==='Overview')body=h(Breakdown,{data,mode:displayMode,onDrill:drill,group:breakdownGroup,setGroup:setBreakdownGroup})
   if(tab==='Requests')body=h(RequestView,{key:pageKey+':'+offset,data,offset,details:detailContext,onDrill:drill,more:()=>setOffset(data.next_offset),back:()=>setOffset(Math.max(0,offset-200))})
   if(tab==='Cache & costs')body=h(CacheView,{data,profile:selected,provider,refresh:ledger.hint,onError:setErr})
   if(tab==='Compressions')body=h(CompressionView,{data,mode:compMode,setMode:setCompMode,details:detailContext})

   if(tab==='Models & tasks')body=table(['Provider','Model','Agent','Task','Requests','Known total','Uncached input','Output','Read',h('span',{title:SESSION_WRITE_BASIS},'Writes · calc.'),'Known est. USD'],data.groups.map(g=>[g.provider,g.model,g.agent_kind||'Unattributed',readable(g,'task'),g.attempts,short(g.known.total_tokens),knownTokens(g,'input_tokens'),short(g.known.output_tokens),short(g.known.cache_read_tokens),sessionWrites(g),money(g.known_cost_usd)]),null,
    data.groups.map(g=>JSON.stringify([g.provider,g.model,g.agent_kind,g.task])),
    data.groups.map(g=>({identity:[g.provider,g.model,g.agent_kind,readable(g,'task')].filter(Boolean).join(' · '),value:viewTokens(g,'total_tokens',count)+' known tokens'})))
 }
 if(tab==='Skills usage')body=h(SkillsUsageView,{profile:selected,windowSeconds,params:params.toString(),scope:navigationContext+JSON.stringify(requestFilters),onSession:id=>editRequestFilters({session:id,sessionScope:'exact'}),view:skillsView,setView:setSkillsView,model:skillsModel,setModel:setSkillsModel,skill:skillsSkill,setSkill:setSkillsSkill});
 return h(AnalyticsPane,{},h('style',{},ledgerCss),
  h('section',{id:'au-main-panel',role:'tabpanel','aria-labelledby':mainId(provider),'data-testid':'provider-page','data-provider':provider||'all',className:'au-provider-pane'},
  h('div',{className:'au-upper',tabIndex:0,'aria-label':'Usage summary and filters'},pageHeader,mainNav,
  provider?h(ProviderLimits,{quota,providerId:provider,label,selected,hiddenIds}):null,
  h('div',{className:'au-view-controls'},
   h('div',{className:'au-segment au-mode-buttons',role:'group','aria-label':'Usage display'},...['Cost','Tokens'].map(v=>h('button',{key:v,'aria-pressed':displayMode===v,onClick:()=>{setDisplayMode(v);}},v))),
   h('select',{className:'au-mode-select','aria-label':'Usage display',value:displayMode,onChange:e=>{setDisplayMode(e.target.value);}},...['Cost','Tokens'].map(v=>h('option',{key:v,value:v},v))),
   h('div',{className:'au-segment au-period-buttons',role:'group','aria-label':'Time window'},...Object.entries({'1h':'Past hour','24h':'Past 24h','7d':'7 days','30d':'30 days','90d':'90 days',all:'All recorded',custom:'Custom'}).map(([v,l])=>h('button',{key:v,'aria-pressed':period===v,onClick:()=>changePeriod(v)},l))),
   h('select',{className:'au-period-select','aria-label':'Time window',value:period,onChange:e=>changePeriod(e.target.value)},...Object.entries({'1h':'Past hour','24h':'Past 24h','7d':'7 days','30d':'30 days','90d':'90 days',all:'All recorded',custom:'Custom'}).map(([v,l])=>h('option',{key:v,value:v},l))),
   h('span',{className:'au-muted au-window-label'},data?new Date(data.window.start*1000).toLocaleString()+' — '+new Date(data.window.end*1000).toLocaleString():'')),
  h('div',{className:'au-toolbar au-filters'},
  period==='custom'?h('input',{type:'datetime-local',step:1,value:customStart,'aria-label':'Window start',onChange:e=>{setCustomStart(e.target.value);setFilterTest('')}}):null,
  period==='custom'?h('input',{type:'datetime-local',step:1,value:customEnd,'aria-label':'Window end',onChange:e=>{setCustomEnd(e.target.value);setFilterTest('')}}):null,
  h('select',{'aria-label':'Agent scope',value:agent,onChange:e=>editRequestFilters({agent:e.target.value,subagentId:''})},...Object.entries({'':'All agents',primary:'Primary agents',subagent:'Subagents only',unknown:'Unattributed'}).map(([v,l])=>h('option',{key:v,value:v},l))),
  h('select',{'aria-label':'Project',value:project,onChange:e=>editRequestFilters({project:e.target.value})},h('option',{value:''},'All projects'),...(data?.project_options||[]).map(p=>h('option',{key:p.id,value:p.id,title:p.path||'Metadata unavailable'},p.label+provenance(p)+(p.basis?.includes('working_directory')?' · directory':'')))),
  h('input',{placeholder:aggregate?'Qualified session ID (use drill-down)':'Session ID (optional)',value:session,onChange:e=>editRequestFilters({session:e.target.value}),'aria-label':'Session ID'}),
  session?h('select',{'aria-label':'Session scope',value:sessionScope,onChange:e=>editRequestFilters({sessionScope:e.target.value})},h('option',{value:'family'},'Session + descendants'),h('option',{value:'exact'},'This session only')):null,
  subagentId?h('button',{onClick:()=>editRequestFilters({subagentId:''}),title:subagentId},'Subagent '+(identityLabels.get(subagentId)||subagentId.slice(-14))+' ×'):null,
  (project||agent||session||subagentId)?h('button',{onClick:clearRequestFilters},'Clear filters'):null,
  !aggregate&&h('button',{onClick:()=>test(testId?'stop':'start')},testId?'End test marker':'Start test marker'),
  h('button',{onClick:exportAll,disabled:busy||!data?.summary},busy?'Exporting…':'Export request CSV'),
  data?.tests?.length?h('select',{'aria-label':'Saved tests',defaultValue:'',onChange:e=>chooseTest(e.target.value)},h('option',{value:''},'Saved test windows'),...data.tests.map(t=>h('option',{key:t.id,value:t.id},t.label+provenance(t)+(t.ended?' · ended':' · open')))):null),
  h(Coverage,{data}),
  h(Summary,{data,label,mode:displayMode,agent,onSelectRange:selectChartRange,onSubagents:()=>editRequestFilters({agent:agent==='subagent'?'':'subagent',subagentId:''})})),
  subNav,
  h(Reader,{resetKey:navigationContext+JSON.stringify(requestFilters)+tab+offset,id:'au-subpage-panel',role:'tabpanel',label:tab+' records','aria-labelledby':subId(tab),'data-testid':'provider-subpage','data-subpage':tab},
  tab==='Requests'?h(RequestNavigation,{filters:requestFilters,history:drillHistory,identityLabels,projects:data?.project_options,onBack:returnFromDrill,onShowAll:showAllRequests,onRemove:removeRequestFilter}):null,
  err?notice(err):null,
  ledger.error?h('p',{className:'au-muted',role:'alert','data-testid':'usage-stale'},data?'Showing the last good recorded usage; refresh failed. Retrying automatically. '+String(ledger.error.message||ledger.error):'Usage unavailable; retrying automatically. '+String(ledger.error.message||ledger.error)):null,
  tab==='Cache & costs'?h(CacheWindowFilters,{period,onPeriod:changePeriod,startValue:customStart,endValue:customEnd,onStart:v=>{setCustomStart(v);setFilterTest('')},onEnd:v=>{setCustomEnd(v);setFilterTest('')},window:data?.window}):null,
  body||h('p',{className:'au-muted'},data?.summary===null?'Recorded usage unavailable for this selection.':'Waiting for ledger…'))))
}


function UsageChip() {
  const hiddenIds = useValue($hidden)
  const selected = useValue($profile)
  const pinnedId = useValue($chipProvider)
  const { data } = useUsage(selected, REFRESH_CHIP_MS)

  // A pin only wins while it resolves to a visible provider with a numeric
  // window; hidden, absent for this profile, or quota-less falls back to AUTO
  // instead of blanking the chip.
  const visible = (data?.providers || []).filter(provider => !(hiddenIds || []).includes(provider.id))
  const pinned = pinnedId ? visible.find(provider => provider.id === pinnedId) : null
  const worst = (pinned ? worstWindowOf(pinned) : null) || worstRemaining(data, hiddenIds)
  const tone = toneFor(worst ? worst.remaining : null)
  const detail = worst
    ? [worst.provider, worst.window ? `${worst.window} window` : null, resetLabel(worst.reset_at)]
        .filter(Boolean)
        .join(' · ')
    : 'AI usage +'

  return jsx('button', {
    type: 'button',
    title: `${detail}${pinned ? ' (pinned on the AI usage + page)' : ''}`,
    'aria-label': `AI usage +: ${detail}`,
    className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)',
    onClick: () => {
      haptic('tap')
      host.navigate(ROUTE)
    },
    children: jsxs('span', {
      className: 'inline-flex items-center gap-1.5',
      children: [
        jsx('span', {
          'aria-hidden': true,
          className: 'inline-block size-1.5 rounded-full',
          style: {
            background:
              tone === 'bad'
                ? 'var(--ui-accent)'
                : tone === 'warn'
                  ? 'var(--ui-text-secondary)'
                  : 'var(--ui-text-quaternary)'
          }
        }),
        jsx('span', { children: worst ? `${pct(worst.remaining)} · ${worst.provider}` : data ? 'AI usage +' : 'AI usage +…' })
      ]
    })
  })
}

export default {
  id: ID,
  name: 'AI usage +',
  register(ctx) {
    rest = ctx.rest
    socket = ctx.socket
    storage = ctx.storage
    pluginOs = ctx.os || null
    try {
      const saved = storage?.get(HIDDEN_KEY, [])
      if (Array.isArray(saved)) $hidden.set(saved.filter(value => typeof value === 'string'))
    } catch {
      /* a malformed stored list must not block the plugin from loading */
    }
    try {
      const savedChip = storage?.get(CHIP_KEY, '')
      if (typeof savedChip === 'string') $chipProvider.set(savedChip)
    } catch {
      /* fall through to AUTO */
    }
    try {
      const savedProfile = storage?.get(PROFILE_KEY, '')
      if (typeof savedProfile === 'string' && savedProfile) {
        $profile.set(savedProfile)
      } else {
        // First run: default to the profile the app itself is on.
        const active = host.state.profile?.get?.()
        if (typeof active === 'string' && active) $profile.set(active)
      }
    } catch {
      /* fall through to the backend's own profile */
    }

    try { if (storage?.get(PROFILE_SCOPE_KEY, 'selected') === 'all') $profile.set(ALL_PROFILES) } catch {}

    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: ROUTE },
      render: () => jsx(UsagePage, {})
    })

    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      data: { path: ROUTE, label: 'AI usage +', codicon: 'graph' }
    })

    ctx.register({
      id: 'chip',
      area: STATUSBAR_AREAS.right,
      order: 125,
      render: () => jsx(UsageChip, {})
    })
  }
}
