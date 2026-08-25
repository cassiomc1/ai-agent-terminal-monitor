# Melhorias sugeridas para o monitoramento

Estas sugestões vieram do uso do monitor acompanhando o OpenCode em um
projeto real, com tarefas que exigiram retomada, permissões, CI, PRs e
mesclagens.

## Prioridade alta

### 1. Separar “inatividade visual” de “processo trabalhando”

O terminal pode permanecer em `Thinking`, `Preparing edit` ou `gh ... --watch`
por vários minutos enquanto há processos filhos consumindo CPU. O monitor deve
correlacionar o snapshot do terminal com processos descendentes, CPU e idade do
comando antes de enviar uma retomada. Isso evita interromper testes ou workflows
válidos.

### 2. Tornar a retomada idempotente e observável

Cada envio automático deve receber um `attempt_id`, motivo, timestamp e estado
observado. O estado deve distinguir `queued`, `sent`, `accepted`, `completed` e
`ignored`, com cooldown por tentativa. Assim uma mensagem não fica presa na fila
sem que o monitor saiba se foi processada.

### 3. Adicionar um protocolo explícito de “bloqueado por CI”

Cancelamentos no limite de tempo, falhas de rede e respostas `429`/timeout de
sites externos não são equivalentes a falhas do código. O monitor deve
classificar cada check como `passed`, `failed`, `cancelled-infra` ou
`failed-external`, repetir somente o job afetado e registrar a evidência usada
para a decisão.

### 4. Impor o gate de mesclagem no próprio monitor

Antes de mesclar, consultar novamente o SHA completo de `headRefOid` e exigir
que todos os checks daquele SHA estejam concluídos com sucesso. Cancelamentos
não devem ser tratados como sucesso nem permitir merge automático. Depois da
mesclagem, verificar o SHA de `main`, workflows pós-merge e árvore limpa.

## Prioridade média

### 5. Detectar alterações fora do branch esperado

Se o agente voltar para `main` com mudanças locais, ou mudar de branch durante a
execução, o monitor deve pausar o fluxo e solicitar uma ação segura: preservar,
commitar em branch, ou descartar somente com autorização explícita. O status
deve mostrar branch, SHA, arquivos modificados e PR associado.

### 6. Usar uma política de permissões por risco

Permissões seguras e repetíveis podem ser aprovadas automaticamente, mas ações
irreversíveis (publicação npm, criação de release, exclusões e mudanças fora do
projeto) devem ser bloqueadas por uma regra independente do texto enviado ao
agente. A proibição de npm deve aparecer também no estado persistido e no
relatório final.

### 7. Dar suporte a retomada após reinício do monitor

Persistir a sessão do agente, último prompt, branch, PR, SHA e etapa do
workflow. Ao reiniciar, reconstruir o estado a partir do GitHub e do terminal,
em vez de enviar novamente uma instrução que pode já estar em processamento.

## Prioridade baixa

### 8. Melhorar o relatório final

Gerar um resumo estruturado com tarefas concluídas, prompts enviados,
permissões decididas, PRs/commits mesclados, checks pós-merge, eventuais
cancelamentos de infraestrutura e confirmação explícita de que não houve
publicação npm.

### 9. Cobrir o supervisor com testes de cenários

Adicionar testes simulados para: agente parado, comando longo ativo, prompt de
permissão, mensagem enfileirada, check cancelado, `429` externo, SHA alterado,
PR já mesclado e reinício do monitor. Esses cenários são mais importantes que
apenas testar a extração de texto do terminal.

### 10. Expor um modo de simulação

Um modo `--dry-run` deve mostrar qual ação o monitor tomaria, sem enviar teclas,
aprovar permissões ou alterar o GitHub. Isso facilita validar políticas antes de
usar o supervisor em um projeto novo.

## Protecoes operacionais implementadas

- O ledger diferencia entrega aceita de mensagem ainda visivelmente `QUEUED`, impede reenvio duplicado e escala filas vencidas para atencao humana.
- O supervisor detecta suites/builds caros duplicados, repeticoes sem progresso observavel e comandos de reescrita de historico Git.
- A interrupcao validada encerra a arvore inteira do comando, dos descendentes mais profundos ao processo solicitado, sem sinalizar o agente raiz; a recuperacao aguarda a saida, escala para `SIGTERM` e nao injeta um prompt enquanto qualquer descendente ainda estiver vivo.
- As deteccoes sao exportadas no status estruturado e podem ser ajustadas por configuracao, mantendo defaults fail-closed.
- O painel web local expõe somente projeções seguras, mostra um snapshot de terminal mascarado e mantém o log rotacionado para evitar vazamento ou crescimento ilimitado.

## Implementação desta rodada

As cinco melhorias priorizadas a partir do uso real e o hardening observado na
revisão do command center foram implementados:

- desligamento por sinal com heartbeat final, PID lock e detecção de estado
  obsoleto;
- inspeção `--once` JSON-safe, limitada e com mascaramento de credenciais;
- painel `status` colorido com tarefa, progresso, comando atual, Git, CI e
  política npm;
- command center web escuro com snapshot de terminal redigido, status sem
  prompts/comandos e inicialização que libera o lock mesmo quando a porta
  escolhida não pode ser usada;
- documentação operacional dos endpoints `/api/status`, `/api/events` e
  `/api/terminal`, dos arquivos de estado redigidos/rotacionados e do ciclo
  `SIGINT` → espera → `SIGTERM` da recuperação de loops;
- comandos `stop`, `status` e `resume` que nunca sinalizam o processo do
  agente;
- testes de regressão e documentação no README/ABOUT para o novo ciclo de vida.
