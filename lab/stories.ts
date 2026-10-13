export interface ProjectBundle { version: 2; files: Record<string, string>; config: Record<string, unknown> }
export interface Story { id: string; title: string; summary: string; question: string; broken: ProjectBundle; repaired: ProjectBundle }
const fields = { county: { type: 'string', required: true, nullable: false }, residents: { type: 'integer', required: true, nullable: false } };
function project(source: string, contracts: object[]): ProjectBundle { return { version: 2, files: { 'pipeline.py': source }, config: { version: 2, contracts } }; }
const connection = [{ kind: 'connection', id: 'validate-before-publication', entry: 'pipeline:app', target: 'pipeline:Publisher.validate' }];
const connectionSource = `import typer

app = typer.Typer()

class Publisher:
    def validate(self, county: str) -> None:
        if not county:
            raise ValueError("A county is required")

    def publish(self, county: str) -> dict:
        # The validator exists, but this method never calls it.
        return {"county": county, "residents": 1200}

@app.command()
def publish() -> None:
    publisher = Publisher()
    publisher.publish("Example County")

if __name__ == "__main__":
    app()
`;
const handoff = [{ kind: 'handoff', id: 'calculation-reaches-publication', entry: 'pipeline:main', producer: 'pipeline:calculate', consumer: 'pipeline:publish', argument: 'residents' }];
const handoffSource = `from prefect import flow, task

@task
def calculate() -> int:
    return 1200

@task
def publish(residents: int) -> dict:
    return {"county": "Example County", "residents": residents}

@flow
def main() -> dict:
    calculated = calculate.submit()
    # Ordering alone does not deliver calculated's payload.
    published = publish.submit(residents=0, wait_for=[calculated])
    return published.result()
`;
const shape = [{ kind: 'shape', id: 'public-snapshot-fields', source: 'pipeline:publish', mode: 'mapping', fields, allow_extra: false }];
const shapeSource = `from pydantic import BaseModel, ConfigDict

class Snapshot(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    county: str
    residents: int

def publish() -> dict:
    # Public consumers require residents; this key was renamed.
    return {"county": "Example County", "population": 1200}
`;
const coverageSource = `from pydantic import BaseModel

class Snapshot(BaseModel):
    county: str
    residents: int

def validate_snapshot(snapshot: dict) -> None:
    if not snapshot["county"]:
        raise ValueError("A county is required")

def publish() -> dict:
    return {"county": "Example County", "residents": 1200}

def main() -> dict:
    snapshot = publish()
    validate_snapshot(snapshot)
    return snapshot

# This new helper is private, but it still belongs in the coverage inventory.
def _county_label() -> str:
    return "Example County"
`;
const coverageContracts = [
    { kind: 'connection', id: 'validator-connected', entry: 'pipeline:main', target: 'pipeline:validate_snapshot' },
    { kind: 'shape', id: 'published-fields', source: 'pipeline:publish', mode: 'mapping', fields, allow_extra: false },
    { kind: 'shape', id: 'model-fields', source: 'pipeline:Snapshot', mode: 'model', fields, allow_extra: false },
];
const coverageLedger = [
    { kind: 'contract', surface: 'pipeline:main', contracts: ['validator-connected'] },
    { kind: 'contract', surface: 'pipeline:validate_snapshot', contracts: ['validator-connected'] },
    { kind: 'contract', surface: 'pipeline:publish', contracts: ['published-fields'] },
    { kind: 'contract', surface: 'pipeline:Snapshot', contracts: ['model-fields'] },
];
function coverageProject(classifyHelper: boolean): ProjectBundle {
    return project(coverageSource, [...coverageContracts, { kind: 'coverage', id: 'publication-coverage', roots: ['.'], ledger: [...coverageLedger, ...(classifyHelper ? [{ kind: 'internal', surface: 'pipeline:_county_label', reason: 'An internal example label; it neither validates nor publishes the public snapshot.' }] : [])] }]);
}
export const stories: Story[] = [
    { id: 'connection', title: '1. The disconnected validator', summary: 'A Typer command calls a class method. Its validator is defined and tested elsewhere, but the publication path misses it.', question: 'Can the configured production entry reach Publisher.validate?', broken: project(connectionSource, connection), repaired: project(connectionSource.replace('        # The validator exists, but this method never calls it.', '        self.validate(county)'), connection) },
    { id: 'handoff', title: '2. The abandoned calculation', summary: 'A Prefect task calculates a value. Publication waits for it and still publishes a hard-coded value.', question: 'Does calculate’s result reach publish’s residents argument?', broken: project(handoffSource, handoff), repaired: project(handoffSource.replace('    # Ordering alone does not deliver calculated\'s payload.\n', '').replace('residents=0, wait_for=[calculated]', 'residents=calculated'), handoff) },
    { id: 'shape', title: '3. The drifting snapshot', summary: 'A published mapping has a renamed field. The declared public contract and a Pydantic model make the intended shape visible.', question: 'Does each returned shape match the public fields and JSON types?', broken: project(shapeSource, shape), repaired: project(shapeSource.replace('    # Public consumers require residents; this key was renamed.\n', '').replace('"population": 1200', '"residents": 1200'), shape) },
    { id: 'coverage', title: '4. The growing project', summary: 'The existing integration contracts pass. A new private helper appears outside their ledger, so coverage fails until you deliberately connect it to a contract or classify it as internal.', question: 'Does every discovered surface have an explicit, supported classification? Add a function, method, model, or conditional declaration and check again.', broken: coverageProject(false), repaired: coverageProject(true) },
];
