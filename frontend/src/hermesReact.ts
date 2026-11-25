import type * as ReactTypes from 'react';

type HermesReact = typeof ReactTypes;
type HermesHooks = Pick<HermesReact, 'useState' | 'useEffect' | 'useCallback'>;

declare global {
  interface Window {
    __HERMES_PLUGIN_SDK__?: {
      React: HermesReact;
      hooks: HermesHooks;
      fetchJSON: <T>(
        url: string,
        init?: RequestInit,
        options?: { allowUnauthorized?: boolean },
      ) => Promise<T>;
    };
    __HERMES_PLUGINS__?: {
      register?: (name: string, component: ReactTypes.ComponentType<any>) => void;
    };
  }
}

function sdk(): NonNullable<Window['__HERMES_PLUGIN_SDK__']> {
  const value = window.__HERMES_PLUGIN_SDK__;
  if (!value) throw new Error('Hermes SDK indisponível');
  return value;
}

export const React = {
  createElement: ((...args: any[]) =>
    (sdk().React.createElement as (...items: any[]) => any)(
      ...args,
    )) as HermesReact['createElement'],
  get Fragment(): HermesReact['Fragment'] {
    return sdk().React.Fragment;
  },
};

export const useState = ((...args: any[]) =>
  (sdk().hooks.useState as (...items: any[]) => any)(...args)) as HermesReact['useState'];

export const useEffect = ((...args: any[]) =>
  (sdk().hooks.useEffect as (...items: any[]) => any)(...args)) as HermesReact['useEffect'];

export const useCallback = ((...args: any[]) =>
  (sdk().hooks.useCallback as (...items: any[]) => any)(
    ...args,
  )) as HermesReact['useCallback'];

export const fetchJSON = <T>(path: string, init?: RequestInit) =>
  sdk().fetchJSON<T>(path, init, { allowUnauthorized: true });
