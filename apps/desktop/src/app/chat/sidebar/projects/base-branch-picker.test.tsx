import { cleanup, render, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const listBaseBranches = vi.hoisted(() => vi.fn())

vi.mock('@nanostores/react', () => ({ useStore: () => null }))
vi.mock('@/components/ui/button', () => ({
  Button: ({ children, disabled }: { children: ReactNode; disabled?: boolean }) => (
    <button disabled={disabled}>{children}</button>
  )
}))
vi.mock('@/components/ui/codicon', () => ({ Codicon: () => <span /> }))
vi.mock('@/components/ui/command', () => ({
  Command: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CommandEmpty: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CommandGroup: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CommandInput: () => <input />,
  CommandItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CommandList: ({ children }: { children: ReactNode }) => <div>{children}</div>
}))
vi.mock('@/components/ui/popover', () => ({
  Popover: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  PopoverContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  PopoverTrigger: ({ children }: { children: ReactNode }) => <div>{children}</div>
}))
vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        projects: {
          baseBranchNone: 'No branches',
          baseBranchPlaceholder: 'Choose a branch',
          branchOff: () => ({ after: '', before: 'Branch off ' })
        }
      }
    }
  })
}))
vi.mock('@/store/coding-status', () => ({ $repoStatus: {} }))
vi.mock('@/store/projects', () => ({
  listBaseBranches: (...args: unknown[]) => listBaseBranches(...args)
}))

import { BaseBranchPicker } from './base-branch-picker'

describe('BaseBranchPicker', () => {
  beforeEach(() => {
    listBaseBranches.mockReset()
  })

  afterEach(cleanup)

  it('does not automatically retry a completed empty branch load', async () => {
    listBaseBranches.mockResolvedValue([])

    render(<BaseBranchPicker onValueChange={vi.fn()} repoPath="/repo" value="" />)

    await waitFor(() => expect(listBaseBranches).toHaveBeenCalledTimes(1))
    await new Promise(resolve => setTimeout(resolve, 25))
    expect(listBaseBranches).toHaveBeenCalledTimes(1)
  })
})
