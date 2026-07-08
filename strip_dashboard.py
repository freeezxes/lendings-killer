import sys
with open('templates/dashboard.html', 'r') as f:
    lines = f.readlines()

new_lines = []
skip = False
for i, line in enumerate(lines):
    # lines 9 to 48
    if 8 <= i <= 47:
        if i == 8:
            new_lines.append("    <link rel=\"stylesheet\" href=\"/static/css/global.css\">\n")
        continue
        
    # lines 88 to 150
    if 87 <= i <= 149:
        continue
        
    new_lines.append(line)

with open('templates/dashboard.html', 'w') as f:
    f.writelines(new_lines)
