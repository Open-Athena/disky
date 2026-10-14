import { describe, expect, it } from 'vitest'
import { LINEAGES, migrationNamed, migrations, schemaFork, sqliteD1 } from './testD1'

describe('the lineages are the directories under `site/migrations/`', () => {
  it('the base lineage `cw` is one; sorted; files beside them are not lineages', () => {
    expect([LINEAGES.filter(l => l === 'cw' || l.includes('.')), [...LINEAGES].sort()]).toEqual([['cw'], [...LINEAGES]])
  })
})

describe.each(LINEAGES)('the %s lineage', lineage => {
  it('starts at `0001_init.sql`', async () => {
    expect((await migrations(lineage))[0].name).toBe('0001_init.sql')
  })

  it('migrationNamed finds a migration by its name, whatever its number', async () => {
    const names = (await migrations(lineage)).map(m => m.name)
    const named = await migrationNamed(lineage, 'store_scoped_index')
    expect(names.filter(n => /^\d+_store_scoped_index\.sql$/.test(n))).toEqual([named])
    await expect(migrationNamed(lineage, 'no_such_migration')).rejects.toThrow(`${lineage}: 0 migrations named \`no_such_migration\` ()`)
  })

  it('its schema fork: the `deletion_runs` column holding the run’s manifest dir', async () => {
    const { raw } = await sqliteD1(lineage)
    const cols = raw.prepare("SELECT name FROM pragma_table_info('deletion_runs')").all().map(r => r.name)
    expect(cols.filter(c => c === schemaFork(lineage).runDir)).toEqual([schemaFork(lineage).runDir])
  })
})
