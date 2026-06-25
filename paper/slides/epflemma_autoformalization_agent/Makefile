TEX = epflemma_autoformalization_agent.tex
PDF = epflemma_autoformalization_agent.pdf

.PHONY: all clean

all: $(PDF)

$(PDF): $(TEX)
	pdflatex -interaction=nonstopmode -halt-on-error $(TEX)
	pdflatex -interaction=nonstopmode -halt-on-error $(TEX)

clean:
	rm -f *.aux *.log *.nav *.out *.snm *.toc *.vrb $(PDF)
