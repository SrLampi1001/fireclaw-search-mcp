---
model-objective: MiniMax-M3
---
# Agent 
The following document details how the agent must write code and documentation in this repository
## Never
- Ship secrets or try to read .env files
    - Ask the user to fill the .env variables, take the .env.example as the warranty for what .env are avaiable. If you just updated said file, ask the user to fill with the real required secret 
- Wrap documentation and comments in code
    - When writting documentation files, keep paragraphs in one single cohesive and continuous line, never use line breaks to wrap the text for human readability.
    - When writting comments in code, keep then in a single line and short. Variable and naming conventions should be self-explanatory.
- Do not be eager to comply to the user
    - The user lacks the expertise required to make grounded decisions on code decisions. First verify they are correct before complying.
        - **You are lacking in current information**, you were trained in pre 2026 information, if the user says a new version of something you don't know about, look up for the web, since in that case they are probably correct.
## Always
- Make sure what branch you are in before commiting
    - Unless explicitly told by the user, do not commit in the main branch. 
- Make sure to keep the documentation files clean
    - Do not write long prose about decicions and old records on documentation files, keep them clean and use the proper means for the prose and decisions.
- Use sub Agents for extensive web search, keeping your main context window clean.

## Skills
Use the skills in `.agents/skills/` for the adecuate tasks. 