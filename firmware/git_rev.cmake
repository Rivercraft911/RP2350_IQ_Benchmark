# Write OUT = #define GIT_REV "<git describe>" if it changed (called at build time).
execute_process(COMMAND git describe --always --dirty --abbrev=10 WORKING_DIRECTORY ${SRC}
                OUTPUT_VARIABLE REV OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
if(NOT REV)
    set(REV unknown)
endif()
set(TEXT "#define GIT_REV \"${REV}\"\n")
if(EXISTS ${OUT})
    file(READ ${OUT} OLD)
endif()
if(NOT "${OLD}" STREQUAL "${TEXT}")
    file(WRITE ${OUT} "${TEXT}")
endif()
