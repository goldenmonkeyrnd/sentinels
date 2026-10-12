export interface ProjectBundle { version: 1; files: Record<string, string>; config: Record<string, unknown> }
export interface Story { id: string; title: string; summary: string; question: string; broken: ProjectBundle; repaired: ProjectBundle }
const fields = { county: { type: 'string', required: true, nullable: false }, residents: { type: 'integer', required: true, nullable: false } };
function project(source: string, contracts: object[]): ProjectBundle { return { version: 1, files: { 'pipeline.py': source }, config: { version: 1, contracts } }; }
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
export const stories: Story[] = [
    { id: 'connection', title: '1. The disconnected validator', summary: 'A Typer command calls a class method. Its validator is defined and tested elsewhere, but the publication path misses it.', question: 'Can the configured production entry reach Publisher.validate?', broken: project(connectionSource, connection), repaired: project(connectionSource.replace('        # The validator exists, but this method never calls it.', '        self.validate(county)'), connection) },
    { id: 'handoff', title: '2. The abandoned calculation', summary: 'A Prefect task calculates a value. Publication waits for it and still publishes a hard-coded value.', question: 'Does calculate’s result reach publish’s residents argument?', broken: project(handoffSource, handoff), repaired: project(handoffSource.replace('    # Ordering alone does not deliver calculated\'s payload.\n', '').replace('residents=0, wait_for=[calculated]', 'residents=calculated'), handoff) },
    { id: 'shape', title: '3. The drifting snapshot', summary: 'A published mapping has a renamed field. The declared public contract and a Pydantic model make the intended shape visible.', question: 'Does each returned shape match the public fields and JSON types?', broken: project(shapeSource, shape), repaired: project(shapeSource.replace('    # Public consumers require residents; this key was renamed.\n', '').replace('"population": 1200', '"residents": 1200'), shape) },
];
